"""Image context fetching + attachment (Phase 3.6.1).

Mirrors herald_context.py's structure: fetch on demand, cache by URL,
attach to candidate posts at digest time. Skip videos entirely
(per James, 2026-07-05) — capturing meaningful video content costs more
than it's worth, and the thumbnail is "pretty useless, futile" for
synthesis grounding.

Architecture is the same shape as Phase 3.5/3.5.1's quote_context:

  enrich_media_batch(parsed_posts, max_fetches)
    -> walks every post's `media` list, extracts photo URLs, fetches
       descriptions via the vision LLM for URLs not yet cached, writes
       rows to image_context. Best-effort; failures recorded as 'failed'
       rows and never abort the batch. Returns (attempted, ok, failed, skipped).

  attach_media_context(candidates)
    -> for each candidate post, looks up photo-media URLs in image_context,
       mutates the candidate in place: p["_image_description"] = description
       (or p["_image_skipped"] = "video" for filtered media).

  fetch_image_description(url, model)
    -> one OpenRouter vision call. Returns (description, model_used, finish_reason).
       Raises on transport failure.

Vision model input is the URL itself (no download, no base64). pbs.twimg.com
URLs are public, served over HTTPS, and OpenRouter's content-type=image_url
accepts https references directly. If we ever switch to a provider that
needs raw bytes, this is the place to add a download step.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import herald_db


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Default vision model. gpt-4o-mini via OpenRouter is the cheapest reliable
# vision-capable model at the moment; ~$0.00015 per low-res image input.
# Override per-deployment via HERALD_VISION_MODEL.
DEFAULT_VISION_MODEL = os.environ.get("HERALD_VISION_MODEL", "openai/gpt-4o-mini")

# Per-cycle budget for fresh image-context fetches. Collect runs every
# 30min peak / 2h off-peak; we want at most ~10 fresh descriptions per
# cycle to keep collect latency small and cost bounded.
HERALD_MAX_MEDIA_FETCHES_PER_CYCLE = int(
    os.environ.get("HERALD_MAX_MEDIA_FETCHES_PER_CYCLE", "10")
)

# HTTP timeout for the OpenRouter vision call (seconds). Image uploads
# are tiny (~tens of KB for typical Twitter images); 30s is generous.
VISION_TIMEOUT = int(os.environ.get("HERALD_VISION_TIMEOUT", "30"))

# The prompt we send alongside the image. Tighter than free-form — we just
# want a 1-2 sentence caption suitable for digest-narrative grounding.
DESCRIPTION_PROMPT = (
    "Describe this image in 1-2 sentences for a news digest. Focus on what "
    "the image shows factually (people, text visible in the image, charts "
    "with their axes/labels, key visual elements). No editorialising."
)


# ---------- low-level vision call ----------

def _call_vision(url, model=None, timeout=None):
    """One OpenRouter vision call. Returns (description, model_used, finish_reason).

    Raises RuntimeError on transport error or non-2xx HTTP.
    """
    model = model or DEFAULT_VISION_MODEL
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set.")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": DESCRIPTION_PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
        "max_tokens": 200,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://herald.jaaaames.com",
            "X-Title": "Herald",
        },
    )
    timeout = timeout if timeout is not None else VISION_TIMEOUT
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenRouter transport error: {e}") from e
    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {body[:300]}")
    msg = choices[0].get("message", {}) or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        # Some providers return list-form content; flatten to text.
        content = " ".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )
    finish_reason = choices[0].get("finish_reason", "unknown")
    model_used = data.get("model", model)
    return (content.strip(), model_used, finish_reason)


# ---------- per-URL helpers ----------

def fetch_image_description(url, model=None):
    """Fetch a 1-2 sentence description for one image URL.

    Returns (description_str, model_used_str). Raises on transport failure.
    """
    desc, model_used, _finish = _call_vision(url, model=model)
    return desc, model_used


def _extract_photo_urls(post):
    """Return photo-type media URLs from a parsed post's `media` list.

    Skips 'video' entries (per James, 2026-07-05: futile to glean from
    thumbnails, too expensive to extract frames). GIFs: parser sets
    type='animated_gif' for Twitter GIFs; treat those as videos too (same
    reasoning — animated content needs frame extraction to be useful).
    Skipped entries will be marked in the candidate via _image_skipped
    for debugging but don't produce context blocks.
    """
    media = post.get("media") or []
    photos = []
    skipped = []
    for m in media:
        mtype = m.get("type")
        url = m.get("url")
        if not url:
            continue
        if mtype == "photo":
            photos.append(url)
        elif mtype in ("video", "animated_gif"):
            skipped.append((url, mtype))
    return photos, skipped


# ---------- batch-level operations ----------

def enrich_media_batch(posts, max_fetches=None):
    """Eagerly fetch image descriptions for photo media in the post batch.

    Walks each post's media list, extracts photo URLs, skips URLs already
    cached in image_context, fetches via the vision model until the
    per-call budget is exhausted.

    Args:
      posts: list of parsed post dicts (must have `media` list, optionally).
      max_fetches: override for HERALD_MAX_MEDIA_FETCHES_PER_CYCLE.
        None means use the env default.

    Returns: (attempted, ok, failed, skipped) — same shape as
    herald_context.enrich_batch. `attempted` counts real vision calls;
    `ok` counts successful descriptions stored; `failed` counts vision
    calls that raised; `skipped` counts photo URLs not reached because
    the budget ran out (best-effort: leaves them uncached for next time).
    """
    if max_fetches is None:
        max_fetches = HERALD_MAX_MEDIA_FETCHES_PER_CYCLE
    if max_fetches <= 0 or not posts:
        return (0, 0, 0, 0)

    # Gather URLs to consider, deduped and in insertion order.
    seen = set()
    photo_urls = []  # ordered list of URLs to attempt
    for p in posts:
        photos, _skipped = _extract_photo_urls(p)
        for url in photos:
            if url not in seen:
                seen.add(url)
                photo_urls.append(url)
    if not photo_urls:
        return (0, 0, 0, 0)

    # Filter out URLs already cached (success or failed) — never refetch
    # the same URL twice. Operator can manually DELETE rows to force
    # refetch (e.g. after upgrading the vision model).
    conn = herald_db.get_connection()
    cached = herald_db.get_image_descriptions(conn, photo_urls)
    to_fetch = [u for u in photo_urls if u not in cached]
    if not to_fetch:
        conn.close()
        return (0, 0, 0, 0)

    attempted = ok = failed = skipped = 0
    for url in to_fetch:
        if attempted >= max_fetches:
            skipped = len(to_fetch) - attempted
            break
        try:
            description, model_used = fetch_image_description(url)
            if not description:
                herald_db.upsert_image_context(
                    conn, url, "failed", error="empty description", model=model_used,
                )
                attempted += 1
                failed += 1
                continue
            herald_db.upsert_image_context(
                conn, url, "success", description=description, model=model_used,
            )
            attempted += 1
            ok += 1
        except Exception as e:
            err = str(e)[:200]
            herald_db.upsert_image_context(
                conn, url, "failed", error=err, model=DEFAULT_VISION_MODEL,
            )
            attempted += 1
            failed += 1
    conn.commit()
    conn.close()
    return (attempted, ok, failed, skipped)


def attach_media_context(candidates):
    """For each candidate, look up photo descriptions in image_context.

    Mutates each candidate in place:
      candidate['_image_description']  = description string (or None)
      candidate['_image_skipped']      = [(url, mtype), ...] for video/gif
                                            media entries (debug aid; the
                                            per-story cap will ignore them
                                            anyway).

    Idempotent. Best-effort: candidates whose media URLs aren't cached
    just end up with _image_description=None; the synthesiser skips them
    cleanly via the per-story cap logic.

    Phase 3.6.2 (link context) will share this same attach pattern via
    a separate module.
    """
    if not candidates:
        return
    # Collect every URL we want to look up.
    all_urls = []
    for p in candidates:
        photos, skipped = _extract_photo_urls(p)
        all_urls.extend(photos)
        p["_image_skipped"] = skipped  # empty list if none
    if not all_urls:
        # Still set empty _image_skipped on each post for downstream code.
        for p in candidates:
            p.setdefault("_image_description", None)
        return
    conn = herald_db.get_connection()
    descriptions = herald_db.get_image_descriptions(conn, list(set(all_urls)))
    conn.close()
    for p in candidates:
        photos, _ = _extract_photo_urls(p)
        # Pick the first photo's description; per-story renderer will apply
        # the per-story cap (HERALD_IMAGES_PER_STORY) when assembling the
        # stage 2 prompt.
        desc = None
        image_url = None
        for url in photos:
            row = descriptions.get(url) or {}
            if row.get("fetch_status") == "success" and row.get("description"):
                desc = row["description"]
                image_url = url
                break
        p["_image_description"] = desc
        p["_image_url"] = image_url
