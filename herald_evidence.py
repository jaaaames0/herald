"""Targeted, cache-aware evidence gathering for the two-stage digest.

This module deliberately runs *after* Stage 1 has selected a story.  It never
uses ``bird thread`` and never scans an entire capture.  A selected reply or
quote can earn one direct-parent read; selected linked articles and photos can
earn their own small, shared issue budgets.
"""
import json
import os
import re
from urllib.parse import urlsplit

import herald_context
import herald_db
import herald_links
import herald_media


MAX_CONTEXT_READS = int(os.environ.get("HERALD_EVIDENCE_CONTEXT_READS", "10"))
# A quote is often the modern equivalent of a reply: the visible post is the
# comment and the actual subject is one or two quoted posts behind it.  Follow
# a short quote-only chain once a story has earned evidence gathering.  This
# is deliberately much smaller than the old collect-time traversal and shares
# the same issue-wide ``context`` budget.
MAX_QUOTE_CHAIN_DEPTH = int(os.environ.get("HERALD_EVIDENCE_QUOTE_DEPTH", "3"))
MAX_THREAD_FETCHES = int(os.environ.get("HERALD_EVIDENCE_THREAD_FETCHES", "1"))
MAX_THREAD_SELF_REPLIES = int(os.environ.get("HERALD_EVIDENCE_THREAD_SELF_REPLIES", "8"))
MAX_LINK_FETCHES = int(os.environ.get("HERALD_EVIDENCE_LINK_FETCHES", "8"))
MAX_IMAGE_FETCHES = int(os.environ.get("HERALD_EVIDENCE_IMAGE_FETCHES", "4"))
MAX_LINKS_PER_STORY = int(os.environ.get("HERALD_EVIDENCE_LINKS_PER_STORY", "2"))
THREAD_START_RE = re.compile(r"(?:🧵|(?:^|\s)1\s*/\s*(?:\d+|n)\b)", re.IGNORECASE)

# These pages are primarily video/social embeds.  They are not useful article
# evidence and should not spend a fetch budget just because a post links one.
VIDEO_OR_SOCIAL_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com",
    "tiktok.com", "www.tiktok.com", "x.com", "www.x.com", "twitter.com",
    "www.twitter.com", "instagram.com", "www.instagram.com",
})


def new_budget():
    """Mutable per-issue budget, passed across every selected story."""
    return {"context": MAX_CONTEXT_READS, "links": MAX_LINK_FETCHES,
            "images": MAX_IMAGE_FETCHES, "threads": MAX_THREAD_FETCHES}


def _engagement(post):
    metrics = post.get("metrics") or {}
    return int(metrics.get("likes") or 0) + 2 * int(metrics.get("retweets") or 0) + int(metrics.get("replies") or 0)


def _cached_parent(target_id):
    conn = herald_db.get_connection()
    try:
        row = conn.execute(
            "SELECT fetch_status, data FROM quote_context WHERE quoted_tweet_id = ?", (str(target_id),)
        ).fetchone()
    finally:
        conn.close()
    if not row or row["fetch_status"] != "success" or not row["data"]:
        return None
    try:
        return json.loads(row["data"])
    except (TypeError, json.JSONDecodeError):
        return None


def _cached_parent_is_inline(target_id):
    """Whether the cache is Bird's shallow inline quote, not a full read.

    Inline quote cards frequently omit their own ``quotedTweet`` field.  They
    are excellent for the first hop but cannot reliably reveal a quote of a
    quote, so a selected story may earn one refresh to discover that pointer.
    """
    conn = herald_db.get_connection()
    try:
        row = conn.execute(
            "SELECT fetch_status, fetch_error FROM quote_context WHERE quoted_tweet_id = ?", (str(target_id),)
        ).fetchone()
    finally:
        conn.close()
    return bool(row and row["fetch_status"] == "success" and row["fetch_error"] == "inline-from-bird")


def _store_parent(target_id, parsed, error=None):
    conn = herald_db.get_connection()
    try:
        herald_context._store_quote_context(
            conn, str(target_id), "success" if parsed else "failed", data=parsed, error=error,
            source="targeted-story-evidence",
        )
        conn.commit()
    finally:
        conn.close()


def _parent_target(post):
    """Return ``(id, kind)`` for a post's immediately relevant parent."""
    quoted = post.get("quoted_tweet") or {}
    conversation = post.get("conversation") or {}
    target_id = (post.get("quoted_tweet_id") or quoted.get("id") or
                 post.get("in_reply_to_id") or conversation.get("parent_id"))
    kind = "quote" if (post.get("quoted_tweet_id") or quoted.get("id")) else "reply"
    return (str(target_id), kind) if target_id else (None, kind)


def _load_parent(target_id, budget, diagnostics, require_full=False):
    """Read one cached or targeted parent, charging only an actual Bird read."""
    cached = _cached_parent(target_id)
    # Do not mistake a shallow inline card for a complete quote chain. A full
    # read is only warranted after a story has selected this exact quote.
    if cached and not (require_full and _cached_parent_is_inline(target_id)):
        diagnostics["context_cached"] += 1
        return cached
    if budget["context"] <= 0:
        diagnostics["context_skipped"] += 1
        return None

    budget["context"] -= 1
    diagnostics["context_reads"] += 1
    try:
        parsed = herald_context.herald_parser.parse_tweet(herald_context.fetch_parent_raw(target_id))
        _store_parent(target_id, parsed)
        return parsed
    except Exception as exc:
        diagnostics["context_failed"] += 1
        _store_parent(target_id, None, error=str(exc)[:200])
        return None


def _attach_direct_parent(post, budget, diagnostics):
    """Attach a selected post's direct parent and its short quote chain.

    Direct replies stay one hop: reply trees are where over-fetching starts.
    Quotes may continue through quote parents because each hop is usually an
    explicit part of the argument being made by the selected post.
    """
    target_id, kind = _parent_target(post)
    if not target_id:
        return

    parsed = _load_parent(target_id, budget, diagnostics, require_full=(kind == "quote"))
    if not parsed:
        return
    if kind != "quote":
        post["_thread_context"] = [parsed]
        return

    chain = [{"tweet": parsed, "_chain_kind": "quote"}]
    seen = {str(post.get("id")), target_id}
    current = parsed
    # The first parent is depth 1.  A setting of 3 therefore permits the
    # visible quote plus two ancestors, enough for A quotes B quotes C.
    for _depth in range(1, max(1, MAX_QUOTE_CHAIN_DEPTH)):
        next_id, next_kind = _parent_target(current)
        if next_kind != "quote" or not next_id or next_id in seen:
            break
        seen.add(next_id)
        current = _load_parent(next_id, budget, diagnostics, require_full=True)
        if not current:
            break
        chain.append({"tweet": current, "_chain_kind": "quote"})
    post["_quote_context"] = chain


def _attach_local_thread_replies(post, diagnostics):
    """Attach only collected self-replies for an explicit thread starter.

    A 🧵 / 1/N marker is a first-party statement that the author has more to
    say. We deliberately use only already-collected cards here: no `bird
    thread`, no third-party replies, and no additional X budget.
    """
    if not THREAD_START_RE.search(post.get("text") or ""):
        return
    conversation = post.get("conversation") or {}
    conversation_id = conversation.get("id") or post.get("id")
    author_id = (post.get("author") or {}).get("id")
    if not conversation_id or not author_id:
        return
    conn = herald_db.get_connection()
    try:
        rows = conn.execute(
            """SELECT external_id, author_id, author_handle, created_at, text,
                      media_urls, link_urls, metrics, post_type, conversation_id,
                      quoted_tweet_id, in_reply_to_id
               FROM posts
               WHERE source='x' AND conversation_id=? AND author_id=? AND external_id != ?
               ORDER BY created_at ASC LIMIT 16""",
            (str(conversation_id), str(author_id), str(post.get("id"))),
        ).fetchall()
    finally:
        conn.close()
    thread = []
    for row in rows:
        data = dict(row)
        for field in ("media_urls", "link_urls", "metrics"):
            try:
                data[field] = json.loads(data[field]) if data.get(field) else []
            except (TypeError, json.JSONDecodeError):
                data[field] = []
        data["id"] = str(data.pop("external_id"))
        data["media"] = data.pop("media_urls")
        data["links"] = data.pop("link_urls")
        data["author"] = {"id": data.pop("author_id"), "username": data.pop("author_handle")}
        data["conversation"] = {"id": data.get("conversation_id"),
                                "parent_id": data.get("in_reply_to_id"),
                                "is_reply": bool(data.get("in_reply_to_id"))}
        data["quoted_tweet"] = ({"id": data["quoted_tweet_id"], "text": None}
                                if data.get("quoted_tweet_id") else None)
        thread.append(data)
    if thread:
        post["_thread_context"] = thread
        diagnostics["thread_local"] += len(thread)


def _author_thread_only(parsed_posts, source_id, author_id):
    """Keep the contiguous self-reply chain rooted at the selected post.

    An author may later answer third-party replies in the same conversation.
    Those are not the promised 🧵 continuation, so follow `parent_id` from the
    marked starter rather than accepting every post by the same account.
    """
    own = []
    for parsed in parsed_posts:
        if str(parsed.get("id")) == str(source_id):
            continue
        if str((parsed.get("author") or {}).get("id")) != str(author_id):
            continue
        own.append(parsed)
    own.sort(key=lambda item: herald_db.parse_twitter_ts(item.get("created_at")) or 0)
    chain, reachable = [], {str(source_id)}
    pending = list(own)
    # A reply can be returned before its parent; keep making passes until no
    # additional self-reply is connected to the root chain.
    while pending and len(chain) < MAX_THREAD_SELF_REPLIES:
        next_pending, progressed = [], False
        for parsed in pending:
            parent_id = str((parsed.get("conversation") or {}).get("parent_id") or "")
            if parent_id in reachable:
                chain.append(parsed)
                reachable.add(str(parsed.get("id")))
                progressed = True
            else:
                next_pending.append(parsed)
        if not progressed:
            break
        pending = next_pending
    return chain


def _attach_marker_thread(post, budget, diagnostics):
    """Read one explicitly-marked author thread if local context is absent.

    The raw endpoint can return a whole conversation. We cache only the
    selected author's self-replies, so this is not a return to thread crawls.
    """
    if not THREAD_START_RE.search(post.get("text") or "") or post.get("_thread_context"):
        return
    conversation = post.get("conversation") or {}
    conversation_id = conversation.get("id") or post.get("id")
    author_id = (post.get("author") or {}).get("id")
    if not conversation_id or not author_id:
        return
    conn = herald_db.get_connection()
    try:
        cached = conn.execute(
            "SELECT fetch_status, thread_json FROM conversation_context WHERE conversation_id=?",
            (str(conversation_id),),
        ).fetchone()
        if cached and cached["fetch_status"] == "success":
            try:
                parsed = json.loads(cached["thread_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                parsed = []
            thread = _author_thread_only(parsed, post.get("id"), author_id)
            if thread:
                post["_thread_context"] = thread
                diagnostics["thread_cached"] += len(thread)
            return
        if budget.get("threads", 0) <= 0:
            diagnostics["thread_skipped"] += 1
            return
        budget["threads"] -= 1
        diagnostics["thread_remote_fetches"] += 1
        try:
            raw_posts = herald_context.fetch_thread_raw(conversation_id)
            parsed = []
            for raw in raw_posts:
                try:
                    parsed.append(herald_context.herald_parser.parse_tweet(raw))
                except Exception:
                    continue
            thread = _author_thread_only(parsed, post.get("id"), author_id)
            herald_context._store_conv_context(
                conn, str(conversation_id), "success", thread=thread,
                source_tweet_id=str(post.get("id")),
            )
            conn.commit()
            if thread:
                post["_thread_context"] = thread
                diagnostics["thread_remote_success"] += 1
            else:
                diagnostics["thread_remote_empty"] += 1
        except Exception as exc:
            herald_context._store_conv_context(
                conn, str(conversation_id), "failed", error=str(exc)[:200],
                source_tweet_id=str(post.get("id")),
            )
            conn.commit()
            diagnostics["thread_remote_failed"] += 1
    finally:
        conn.close()


def _article_urls(post):
    urls = post.get("link_urls") or post.get("links") or []
    if isinstance(urls, str):
        urls = [urls]
    out = []
    for url in urls:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if url and host not in VIDEO_OR_SOCIAL_HOSTS:
            out.append(url)
    return out


def _attach_articles(posts, budget, diagnostics):
    eligible = []
    for post in sorted(posts, key=_engagement, reverse=True):
        urls = _article_urls(post)
        if urls:
            copy = dict(post)
            copy["link_urls"] = urls[:MAX_LINKS_PER_STORY]
            eligible.append((post, copy))
    if not eligible or budget["links"] <= 0:
        return
    copies = [copy for _, copy in eligible]
    try:
        attempted, ok, failed, skipped = herald_links.enrich_link_batch(copies, max_fetches=budget["links"])
        budget["links"] -= attempted
        diagnostics["link_fetches"] += attempted
        diagnostics["link_success"] += ok
        diagnostics["link_failed"] += failed
        diagnostics["link_skipped"] += skipped
        herald_links.attach_link_context(copies)
        for post, copy in eligible:
            post["_link_context"] = copy.get("_link_context") or []
    except Exception as exc:
        diagnostics["link_error"] = str(exc)[:200]


def _attach_images(posts, budget, diagnostics):
    # Vision is reserved for selected posts whose short text cannot explain a
    # photo by itself.  Video/GIF media are already ignored by herald_media.
    candidates = [p for p in posts if len((p.get("text") or "").strip()) < 280]
    if not candidates or budget["images"] <= 0:
        return
    candidates.sort(key=_engagement, reverse=True)
    try:
        attempted, ok, failed, skipped = herald_media.enrich_media_batch(candidates, max_fetches=budget["images"])
        budget["images"] -= attempted
        diagnostics["image_fetches"] += attempted
        diagnostics["image_success"] += ok
        diagnostics["image_failed"] += failed
        diagnostics["image_skipped"] += skipped
        herald_media.attach_media_context(candidates)
    except Exception as exc:
        diagnostics["image_error"] = str(exc)[:200]


def enrich_story_posts(posts, budget, dry_run=False):
    """Enrich one chosen story in place and return a compact audit record."""
    diagnostics = {
        "post_ids": [str(p.get("id")) for p in posts], "context_reads": 0,
        "context_cached": 0, "context_failed": 0, "context_skipped": 0,
        "thread_local": 0, "thread_cached": 0, "thread_remote_fetches": 0,
        "thread_remote_success": 0, "thread_remote_empty": 0,
        "thread_remote_failed": 0, "thread_skipped": 0,
        "link_fetches": 0, "link_success": 0, "link_failed": 0, "link_skipped": 0,
        "image_fetches": 0, "image_success": 0, "image_failed": 0, "image_skipped": 0,
    }
    if dry_run:
        return diagnostics
    for post in posts:
        _attach_local_thread_replies(post, diagnostics)
        _attach_marker_thread(post, budget, diagnostics)
        _attach_direct_parent(post, budget, diagnostics)
    _attach_articles(posts, budget, diagnostics)
    _attach_images(posts, budget, diagnostics)
    return diagnostics
