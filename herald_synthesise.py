"""Herald two-stage synthesis.

Stage 1: TIMELINE MAP
  Input: 30-50 candidate posts (pre-filtered by herald_embed.prepare_candidates).
  LLM: reads the whole timeline, identifies major themes, lines of debate,
       signal vs noise, and which 5-7 themes are worth writing as stories.
  Output: timeline_map dict.

Stage 2: PER-THEME STORY
  Input: one theme from the timeline map + its constituent posts.
  LLM: writes a multi-paragraph narrative synthesis with inline [post:ID]
       citations, embedded quotes, attribution.
  Output: story dict (compatible with stories table).

Why two stages:
  - Keeps each LLM call's context bounded. Stage 1 sees ~30-50 posts (~10k
    input tokens). Stage 2 sees 5-10 posts (~2-3k input tokens) plus a small
    context block from the timeline map.
  - Lets the LLM focus on one job per call: editorial survey vs. narrative
    writing. The prompts can be sharper.
  - Reasoning models get to reason at each stage: pattern-finding in stage 1,
    narrative craft in stage 2.

Why a reasoning model matters here:
  - Stage 1 needs to find themes ACROSS posts even when they don't share
    links or words. That's pattern recognition + editorial judgement.
  - Stage 2 needs to take the timeline map's framing and produce a
    narrative that connects the theme's posts coherently.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import herald_env
from herald_env import load_herald_env

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("HERALD_MODEL", "openai/gpt-oss-120b")
# Caps are set well above any plausible output (OpenRouter's context window
# for gpt-oss-120b is 128K). At our scale, a runaway output would be the
# afternoon runs hit different failure modes (length cap, then upstream
# so we can still see when an output is unusually large.
# Set to 0 to disable entirely (OpenRouter will use the model default).
STAGE1_MAX_TOKENS = int(os.environ.get("HERALD_STAGE1_MAX_TOKENS", "64000"))
STAGE2_MAX_TOKENS = int(os.environ.get("HERALD_STAGE2_MAX_TOKENS", "64000"))

def _render_context_block(context_posts, max_chars=600):
    """Render an optional list of context posts (parsed tweet dicts) as a compact
    human-readable block for inclusion in the LLM prompt. Includes a header so
    the LLM clearly distinguishes them from candidate posts, and tightens each
    post to id/author/text/has-media/link-hints so context posts don't drown
    out the candidates in token cost.

    Items in `context_posts` may carry a `_depth` integer (0 = top-level /
    direct thread member or depth-1 quote parent; 1 = one level nested in
    a quote chain; etc.). Nested levels are indented with ↳ markers so the
    LLM sees the chain structure when synthesising context-rich stories.

    Returns: (block_text, n_posts)  or  ("", 0) when no context.
    """
    if not context_posts:
        return "", 0
    lines = [f"CONTEXT POSTS ({len(context_posts)} parsed; each shows "
             f"id, author, text — cite with [post:ID] when a claim is grounded by them; "
             f"↳ prefix marks levels nested inside a quote-tweet chain):"]
    for cp in context_posts:
        depth = cp.get("_depth", 0) or 0
        if depth > 0:
            marker = "↳"
            indent = "  " + "  " * (depth - 1)
        else:
            marker = "•"
            indent = "  "
        pid = cp.get("id", "?")
        author = (cp.get("author") or {}).get("username", "unknown")
        text = (cp.get("text") or "").replace("\n", " ")[:max_chars]
        suffix = ""
        if cp.get("media"):
            suffix += f" [{len(cp['media'])} media]"
        if cp.get("links"):
            suffix += f" [{len(cp['links'])} link(s)]"
        lines.append(f"{indent}{marker} [post:{pid}] @{author}{suffix}: {text}")
    return "\n".join(lines), len(context_posts)


def _gather_context_for(posts):
    """Collect _thread_context + _quote_context across a list of candidate posts
    into a single deduped list of context posts for prompt inclusion.

    Posts here are candidate posts (already have _thread_context /
    _quote_context attached by herald_context.attach_context_to_candidates).

    _quote_context may be a single dict (legacy) or a list of dicts ordered
    shallowest-to-deepest (recursive quote chains). Each emitted item gets
    a `_depth` attribute so the rendered block can indent nested levels.

    Dedup by post id (quoted targets may overlap with thread members).
    """
    seen = set()
    out = []
    for p in posts:
        cand_id = p.get("id")
        thread = p.get("_thread_context") or []
        for tp in thread:
            tpid = tp.get("id")
            if not tpid or tpid == cand_id or tpid in seen:
                continue
            seen.add(tpid)
            # Don't mutate the cached thread tweet; copy + tag with depth.
            tagged = dict(tp)
            tagged["_depth"] = 0
            out.append(tagged)
        # _quote_context: support both list (chain) and legacy single-dict.
        qc = p.get("_quote_context")
        if qc is None:
            continue
        if isinstance(qc, list):
            chain = qc
        else:
            chain = [qc]
        for depth, qc_item in enumerate(chain):
            qid = qc_item.get("id")
            if not qid or qid == cand_id or qid in seen:
                continue
            seen.add(qid)
            tagged = dict(qc_item)
            tagged["_depth"] = depth
            out.append(tagged)
    return out


def _to_int(v, default):
    """Coerce max_tokens env var to int; 0 means 'no cap' (omit from payload)."""
    try:
        return int(v) if v else default
    except (TypeError, ValueError):
        return default

TEMPERATURE = float(os.environ.get("HERALD_SYNTHESIS_TEMPERATURE", "0.2"))


def _call(messages, model=None, max_tokens=STAGE2_MAX_TOKENS, temperature=TEMPERATURE, timeout=180):
    """One-shot OpenRouter chat call. Returns (content_str, usage_dict, model_used, finish_reason)."""
    model = model or DEFAULT_MODEL
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set.")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {err_body}") from e
    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {body[:300]}")
    content = choices[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    finish_reason = choices[0].get("finish_reason", "unknown")
    model_used = data.get("model", model)
    # Surface non-stop finish reasons loudly so the next silent failure is
    # visible in digest.log. finish=error = upstream provider rejected the
    # request (transient, rate limit, content filter); finish=length =
    # output truncated by max_tokens (we should not see this anymore).
    if finish_reason != "stop":
        sys.stderr.write(
            f"[openrouter] WARNING: non-stop finish_reason={finish_reason!r} "
            f"model={model_used} content_len={len(content)} usage={dict(usage)} "
            f"body_preview={body[:400]!r}\n"
        )
    return content, usage, model_used, finish_reason


# -------- Stage 1: timeline map --------

STAGE1_SYSTEM = """You are Herald's digest editor. You're reading a curated slice of posts from the last
{window_hours} hours of an X (Twitter) home timeline. Your job is editorial survey, NOT per-post
summarisation. The downstream stage will write stories — your job is to identify WHAT stories
matter and WHY.

Read every post. Then produce a JSON object (no markdown fences, no preamble) with these keys:

  zeitgeist: array of 3-7 theme objects, each:
    {label: short label (≤4 words), description: 1-2 sentence summary, post_ids: [array of cited post IDs],
     accounts: [unique author handles contributing], salience: "high" | "medium" | "low",
     why_it_matters: 1 sentence explaining editorial relevance}

  debate_lines: array of 0-4 debate objects, each:
    {topic: short label, sides: [{position: short label, accounts: [handles], post_ids: [IDs]},
                                  {position: ..., accounts: ..., post_ids: ...}]}
    (only include if there is genuine disagreement across accounts; skip if not)

  signal_vs_noise: {high_signal_themes: [labels from zeitgeist], noise_patterns: [1-3 short observations
                     about what was loud but empty or off-topic, with post_ids as evidence]}

  stories_to_write: array of 3-5 theme labels (subset of zeitgeist) that deserve a narrative story,
                    ordered by importance. For each, suggest: {label, angle: 1 sentence on framing,
                    max_stories: 1 (always 1 in v1)}.

Hard constraints:
- Every post_id you cite MUST be a real post ID from the input. Do not invent IDs.
- Every observation in zeitgeist/debate_lines/signal_vs_noise must cite ≥1 source post.
- No fabrication. If the posts don't establish something, don't say it.
- Synthesis ACROSS posts is the whole point — name multiple accounts and posts when they corroborate.
- 'Salience' is YOUR editorial judgement, not just engagement. A low-engagement post that surfaces
  a substantive argument can be high salience. A high-engagement post that's empty chatter is low.

Context posts: a separate "CONTEXT POSTS" block may follow the main POSTS list. These are
parsed replies / parents of reply-candidates fetched via bird thread. They are ELEGIBLE for
citation — cite them like any other post when they ground a claim. Prefer the candidate
post's own citation when it directly states the claim, since the candidate is what the
user saw.
"""


def stage1_timeline_map(candidate_posts, window_hours=12, model=None, dry_run=False):
    """Stage 1: produce the timeline map from a curated candidate set.

    candidate_posts: list of post dicts (must include 'id', 'author.username', 'text', 'metrics').
    Returns: (timeline_map_dict, usage_dict, model_used, finish_reason)
    """
    if dry_run:
        # Synthetic placeholder so we can dry-run the rest of the pipeline.
        n = len(candidate_posts)
        return {
            "zeitgeist": [{"label": "[DRY-RUN]", "description": "no LLM call", "post_ids": [],
                           "accounts": [], "salience": "low", "why_it_matters": "dry-run"}],
            "debate_lines": [],
            "signal_vs_noise": {"high_signal_themes": [], "noise_patterns": []},
            "stories_to_write": [{"label": "[DRY-RUN]", "angle": "no LLM call", "max_stories": 1}],
        }, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}, "dry-run", "stop"

    posts_serialised = []
    for p in candidate_posts:
        m = p.get("metrics") or {}
        posts_serialised.append({
            "id": p.get("id"),
            "author": (p.get("author") or {}).get("username", "unknown"),
            "text": (p.get("text") or "")[:800],
            "metrics": m,
            "created_at": p.get("created_at"),
        })

    # .replace() instead of .format() because the system prompt contains
    # many literal {label}/{position} placeholders in the JSON examples that
    # str.format() would try to interpolate as Python keywords.
    system = STAGE1_SYSTEM.replace("{window_hours}", str(window_hours))

    context_posts = _gather_context_for(candidate_posts)
    context_block, n_ctx = _render_context_block(context_posts)

    user_parts = [
        f"POSTS ({len(posts_serialised)} curated candidates from last {window_hours}h):\n"
        + json.dumps(posts_serialised, ensure_ascii=False)
    ]
    if context_block:
        user_parts.append(context_block)
    user_parts.append("Produce the timeline map JSON now.")
    user = "\n\n".join(user_parts)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    content, usage, model_used, finish_reason = _call(messages, model=model, max_tokens=STAGE1_MAX_TOKENS)

    # Parse; best-effort. Carry finish_reason forward so the DB row preserves
    # the diagnostic (length vs error vs content_filter vs etc.).
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        obj = {
            "_parse_warning": "model returned non-JSON",
            "_raw": content[:1000],
            "_finish_reason": finish_reason,
        }
    # Also annotate if finish_reason itself is the problem (e.g. finish=error
    # with empty content — JSON parse would "succeed" against "" and give {})
    if finish_reason != "stop" and not obj.get("_parse_warning"):
        obj["_finish_reason"] = finish_reason


    return obj, usage, model_used, finish_reason


# -------- Stage 2: per-theme story --------

STAGE2_SYSTEM = """You are Herald's digest writer. You're writing a 1-3 paragraph narrative story for a
daily news digest, synthesising a cluster of social-media posts into a coherent read. The upstream
"timeline map" has flagged this cluster as a story worth telling and suggested a framing angle.

SOURCE-GROUNDING RULES (HARD CONSTRAINTS):
1. Every factual claim MUST cite a source post inline as [post:ID], where ID is the post's `id`
   from the POSTS block. No bare assertions.
2. Direct quotes must be embedded verbatim with attribution: "quoted text" — @author.
3. Paraphrasing is fine. Fabrication is not. If the posts don't establish something, don't say it.
4. Don't repeat information already established by an earlier cited post — chain citations cleanly.

Output a JSON object (no markdown fences, no preamble) with keys:
  headline: short string ≤ 90 chars, captures the story.
  story:    1-3 paragraphs of narrative prose with inline [post:ID] citations.
  topic:    short label (e.g. 'markets', 'ai', 'policy').
  key_quotes: array of {post_id, quote, speaker} — quotes you actually embedded in the story.
  coverage_type: 'one_shot' (default) or 'ongoing'.
  section: 'story' (always — distinguishes this row from zeitgeist/debate entries in the DB).

Context posts: an optional CONTEXT POSTS block may follow the POSTS. These are reply parents
or quote targets fetched via bird thread/read so the writer can see the conversation a reply
sits within, or what a quote-tweet is quote-tweeting. Cite context posts with the same
[post:ID] convention when a claim is grounded by them.
"""


def stage2_story(theme_label, theme_posts, angle=None, timeline_map_summary=None,
                 model=None, dry_run=False):
    """Stage 2: write a story for one theme.

    theme_label: the theme's label from the timeline map.
    theme_posts: list of post dicts (the posts that contributed to this theme).
    angle: 1-sentence framing suggested by the timeline map (optional).
    timeline_map_summary: small context block from the timeline map (optional).

    Returns: (story_dict, usage_dict, model_used, finish_reason)
    """
    if dry_run:
        return {
            "headline": f"[DRY-RUN] Story on {theme_label}",
            "story": f"[DRY-RUN: would synthesise {len(theme_posts)} post(s) on '{theme_label}' into a narrative.]",
            "topic": "dry-run",
            "key_quotes": [],
            "media_references": [],
            "coverage_type": "one_shot",
            "section": "story",
            "model_version": "dry-run",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        }, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}, "dry-run", "stop"

    posts_serialised = []
    for p in theme_posts:
        posts_serialised.append({
            "id": p.get("id"),
            "author": (p.get("author") or {}).get("username", "unknown"),
            "text": (p.get("text") or "")[:1500],
            "metrics": p.get("metrics") or {},
            "links": p.get("links") or [],
        })
    angle_line = f"\nFraming angle (from the timeline map): {angle}" if angle else ""
    context_line = f"\nTimeline context: {timeline_map_summary}" if timeline_map_summary else ""

    context_posts = _gather_context_for(theme_posts)
    context_block, _ = _render_context_block(context_posts)

    user_parts = [
        f"THEME: {theme_label}{angle_line}{context_line}",
        f"POSTS ({len(posts_serialised)}):\n{json.dumps(posts_serialised, ensure_ascii=False)}",
    ]
    if context_block:
        user_parts.append(context_block)
    user_parts.append("Write the story JSON now.")
    user = "\n\n".join(user_parts)
    messages = [{"role": "system", "content": STAGE2_SYSTEM}, {"role": "user", "content": user}]
    content, usage, model_used, finish_reason = _call(messages, model=model)

    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        obj = {
            "headline": f"Story on {theme_label}",
            "story": content,
            "topic": "unparsed",
            "key_quotes": [],
            "_parse_warning": "model returned non-JSON",
        }

    obj.setdefault("topic", "")
    obj.setdefault("key_quotes", [])
    obj.setdefault("media_references", [])
    obj.setdefault("coverage_type", "one_shot")
    obj["section"] = "story"
    obj["theme_label"] = theme_label
    obj["angle"] = angle  # the framing angle suggested by stage 1 (may be None)
    obj["model_version"] = model_used
    obj["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    obj["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    obj["cost_usd"] = float(usage.get("cost") or 0.0)
    obj["finish_reason"] = finish_reason
    return obj, usage, model_used, finish_reason
