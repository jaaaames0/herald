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
import re
import sys
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("HERALD_MODEL", "openai/gpt-oss-120b")
STAGE1_MODEL = os.environ.get("HERALD_EDITOR_MODEL", DEFAULT_MODEL)
STAGE2_MODEL = os.environ.get("HERALD_WRITER_MODEL", DEFAULT_MODEL)
# These are editorial output budgets, not context-window limits.  A digest
# should fail visibly and cheaply before a reasoning model spends 64k tokens
# wandering toward JSON.  Operators may override them in the environment.
def _bounded_int(name, default, maximum):
    """Accept operator tuning without permitting an accidental 64k runaway."""
    try:
        return min(int(os.environ.get(name, str(default))), maximum)
    except ValueError:
        return default


def parse_json_response(content):
    """Parse a provider response, tolerating a harmless ```json wrapper."""
    text = (content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as original_error:
        # Some providers emit valid top-level fields but forget to escape a
        # quotation mark inside long prose. Recover only the unambiguous
        # headline/story/topic framing; normal citation and quote validation
        # still runs in stage2_story before anything can be published.
        headline_match = re.search(
            r'"headline"\s*:\s*"(?P<head>(?:\\.|[^"\\])*)"\s*,\s*"story"\s*:', text, re.DOTALL,
        )
        story_match = re.search(
            r'"story"\s*:\s*"(?P<story>.*)"\s*,\s*"topic"\s*:\s*"(?P<topic>(?:\\.|[^"\\])*)"',
            text, re.DOTALL,
        )
        if not headline_match or not story_match:
            raise original_error

        def loose_string(value):
            # Decode standard escaped sequences where possible, but preserve
            # the unescaped prose quotes that caused the original failure.
            try:
                return json.loads('"' + value + '"', strict=False)
            except json.JSONDecodeError:
                return (value.replace('\\"', '"').replace('\\n', '\n')
                        .replace('\\t', '\t').replace('\\\\', '\\'))

        recovered = {
            "headline": loose_string(headline_match.group("head")),
            "story": loose_string(story_match.group("story")),
            "topic": loose_string(story_match.group("topic")),
            "key_quotes": [],
            "_parse_recovered": "malformed provider JSON; recovered top-level prose fields",
        }
        return recovered


STAGE1_MAX_TOKENS = _bounded_int("HERALD_STAGE1_MAX_TOKENS", 6000, 8000)
STAGE2_MAX_TOKENS = _bounded_int("HERALD_STAGE2_MAX_TOKENS", 2600, 4000)

# Reasoning-token caps (Phase 3.8.1 fix). Reasoning models (gpt-oss-120b) otherwise
# burn the full max_tokens budget on chain-of-thought and emit empty JSON. Without
# these caps, digest 32 (2026-07-10 morning) produced `{"final{"` plus whitespace
# at finish_reason=length, costing $0.025 for nothing. Defaults: stage 1 = 4000,
# stage 2 = 2000. Override via env if you need different ceilings.
# OpenRouter's current API controls gpt-oss reasoning with ``reasoning.effort``.
# The older top-level max_reasoning_tokens field is ignored by providers for this
# model, which is why a nominal 700-token cap still consumed 1,797 tokens.
STAGE1_REASONING_EFFORT = os.environ.get("HERALD_STAGE1_REASONING_EFFORT", "low")
STAGE2_REASONING_EFFORT = os.environ.get("HERALD_STAGE2_REASONING_EFFORT", "low")
# Compatibility selectors for existing callers. These numbers are no longer
# sent to OpenRouter as token caps; see _call above.
STAGE1_MAX_REASONING_TOKENS = 700
STAGE2_MAX_REASONING_TOKENS = 600

def _render_context_block(context_posts, max_chars=600):
    """Render an optional list of context posts (parsed tweet dicts) as a compact
    human-readable block for inclusion in the LLM prompt. Includes a header so
    the LLM clearly distinguishes them from candidate posts, and tightens each
    post to id/author/text/has-media/link-hints so context posts don't drown
    out the candidates in token cost.

    Items in `context_posts` may carry:
      - `_depth` int: 0 = top-level (thread member or depth-1 quote target of
        our post); 1+ = nested in some chain. Indented with `↳`.
      - `_chain_kind` str: 'quote' or 'reply'. Quote chain items use `↳`,
        reply chain items use `⤴` so the LLM can tell the two dimensions
        apart in tangled chains (quote-of-reply-of-quote patterns).

    Returns: (block_text, n_posts)  or  ("", 0) when no context.
    """
    if not context_posts:
        return "", 0
    lines = [f"CONTEXT POSTS ({len(context_posts)} parsed; each shows "
             f"id, author, text — cite with [post:ID] when a claim is grounded by them; "
             f"• marks the chain head, ↳ marks nested quote-tweet depth, "
             f"⤴ marks reply-parent edges):"]
    for cp in context_posts:
        depth = cp.get("_depth", 0) or 0
        kind = cp.get("_chain_kind", "quote")
        if kind == "reply":
            marker = "⤴"
            indent = "  "
        elif depth > 0:
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

    _quote_context may be:
      - a single dict (legacy format from before Phase 3.5) — treated as a
        one-element chain with _chain_kind='quote'
      - a list of {"tweet": parsed_dict, "_chain_kind": "quote"|"reply"}
        dicts, ordered by traversal (DFS through both quote and reply dims)

    Each emitted item gets `_depth` (0 for top-level, incremented on each
    quote hop) and `_chain_kind` (preserved from the input) so the rendered
    block can distinguish nested quote levels from reply edges.

    Dedup by post id (quoted targets may overlap with thread members; reply
    parents may overlap with quote targets).
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
            tagged = dict(tp)
            tagged["_depth"] = 0
            tagged["_chain_kind"] = "thread"
            out.append(tagged)
        qc = p.get("_quote_context")
        if qc is None:
            continue
        # Normalise to a list of {"tweet": ..., "_chain_kind": ...} items.
        if isinstance(qc, dict):
            chain_items = [{"tweet": qc, "_chain_kind": "quote"}]
        elif isinstance(qc, list):
            if qc and isinstance(qc[0], dict) and "tweet" in qc[0]:
                # New format: list of {"tweet": ..., "_chain_kind": ...}
                chain_items = qc
            else:
                # Legacy format: list of parsed tweet dicts
                chain_items = [{"tweet": t, "_chain_kind": "quote"} for t in qc]
        else:
            continue
        quote_depth = 0
        for item in chain_items:
            kind = item.get("_chain_kind", "quote")
            tweet = item.get("tweet", {})
            qid = tweet.get("id")
            if not qid or qid == cand_id or qid in seen:
                continue
            seen.add(qid)
            tagged = dict(tweet)
            tagged["_chain_kind"] = kind
            if kind == "quote":
                tagged["_depth"] = quote_depth
                quote_depth += 1
            else:
                tagged["_depth"] = 0  # reply parents sit at their own depth
            out.append(tagged)
    return out


TEMPERATURE = float(os.environ.get("HERALD_SYNTHESIS_TEMPERATURE", "0.2"))


def _call(messages, model=None, max_tokens=STAGE2_MAX_TOKENS, max_reasoning_tokens=None,
          temperature=TEMPERATURE, timeout=180):
    """One-shot OpenRouter chat call. Returns (content_str, usage_dict, model_used, finish_reason).

    ``max_reasoning_tokens`` is retained for caller compatibility. For gpt-oss
    OpenRouter now accepts an effort setting instead; Stage 1 and Stage 2 pass
    their respective low-effort settings below.
    """
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
    if max_reasoning_tokens is not None:
        effort = STAGE1_REASONING_EFFORT if max_reasoning_tokens == STAGE1_MAX_REASONING_TOKENS else STAGE2_REASONING_EFFORT
        payload["reasoning"] = {"effort": effort, "exclude": True}
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
summarisation — and crucially, NOT forced output. The downstream stage will write stories and
items; your job is to identify WHAT to cover and at WHAT shape.

You are the editor, not a formatter. You decide what's worth covering today, at what depth,
in what form. If today genuinely has only three angles, three stories is the right answer.
The finished issue has room for at most 12 entries. Choose the best 3–6 stories and at most
6 items; skip the rest. Don't pad or artificially broaden to hit a count. Coverage is a
judgement, not an inventory of every candidate.

Two coverage shapes available to you:

- **Stories** — multi-post clusters where the writer (stage 2) will produce a 1-3 paragraph
  narrative. The writer adapts depth to how much source material the cluster has; you decide
  whether the cluster deserves a story at all.

- **Items** — single posts worth surfacing on their own merits but not worth a paragraph each.
  High-engagement singletons, off-beat observations, short witty replies, sidebar observations,
  news items that landed cleanly in one tweet, funny quotes, memes. The writer will give each
  a 1-2 sentence caption or pass the tweet through verbatim.

Read every post. Then produce a JSON object (no markdown fences, no preamble) with these keys:

  zeitgeist: array of 0-6 theme objects — the clusters where multi-post stories make sense.
    Each: {label: short label (≤4 words), description: one short sentence,
           post_ids: [array of cited post IDs], accounts: [unique author handles contributing],
           salience: "high" | "medium" | "low", why_it_matters: 1 sentence explaining
           editorial relevance, ≤18 words}.

  stories_to_write: array of theme labels (subset of zeitgeist) that should be turned into
                    stories, ordered by importance. For each: {label, angle: 1 sentence on
                    framing, max_stories: 1 (always 1 — one story per theme)}. If only one
                    theme is strong today, this has one entry. If three are strong, three.
                    If zero themes are strong, this is an empty array — surface items instead.

  items: OPTIONAL array of single-post picks from the candidates. Each:
    {post_id: cited post ID, why: 1 sentence on why it deserves a slot,
     kind: your own tag — 'news', 'humor', 'quote', 'meme', 'off-beat', 'link', 'sidebar',
           'observation', 'thread-anchor', whatever fits. kind is for the writer's framing,
           not a constraint.}
    Pick 0-6 items. ``why`` may describe only what the post itself establishes;
    do not call it viral, Australian, merchandise, a reaction, or a broader trend
    unless the post says so. If a candidate didn't form
    a theme but is still worth surfacing (high engagement, off-beat, a clean one-tweet
    news hit, a sharp joke), put it here. If today's themes already cover everything
    interesting, omit items entirely. Items and stories are independent — you can have
    items without stories, stories without items, both, or neither.

  editor_notes: OPTIONAL one short sentence to the writers (stage 2). Set the
                tone for today's digest: what's the dominant register (urgent news cycle?
                quiet day? comedy-leaning? tech release day?). What to lean into. What to
                skip. The writers will use this as direction, not as a hard rule.

Hard constraints:
- Every post_id you cite MUST be a real post ID from the input. Do not invent IDs.
- Every observation in zeitgeist must cite ≥1 source post.
- Every item must cite exactly one post_id.
- No fabrication. If the posts don't establish something, don't say it.
- Synthesis ACROSS posts is the whole point for stories — name multiple accounts and posts
  when they corroborate. For items, one post_id per item is the rule.
- A story needs one specific shared event, argument, or cultural moment. Never make a broad
  sector roundup: a lawsuit, a security anecdote, and an unrelated political post are separate
  items, not "AI legal and safety fallout". If the common subject needs a vague category label,
  do not make a story.
- 'Salience' is YOUR editorial judgement, not just engagement. A low-engagement post that
  surfaces a substantive argument can be high salience. A high-engagement post that's empty
  chatter is low. Same logic for items — engagement is a hint, not the criterion.
- Off-beat, single-account, weird, sideways moments are welcome on their own merits. A
  singleton can be a story (when strong enough), an item (when interesting but not
  narratable), or omitted (when genuinely nothing there). Your call.
- Don't fabricate breadth. If the timeline is dominated by 3 topics and nothing else,
  don't invent a 4th to seem diverse.

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
            "stories_to_write": [{"label": "[DRY-RUN]", "angle": "no LLM call", "max_stories": 1}],
            "items": [],
            "editor_notes": "",
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
    content, usage, model_used, finish_reason = _call(
        messages, model=model or STAGE1_MODEL,
        max_tokens=STAGE1_MAX_TOKENS,
        max_reasoning_tokens=STAGE1_MAX_REASONING_TOKENS,
    )

    # Parse; best-effort. Carry finish_reason forward so the DB row preserves
    # the diagnostic (length vs error vs content_filter vs etc.).
    try:
        # Some otherwise excellent writers emit literal newlines in JSON
        # string fields. OpenRouter returns the content intact; strict=False
        # accepts those control characters without weakening our citation and
        # quote validation below.
        obj = parse_json_response(content)
    except json.JSONDecodeError:
        obj = {
            "_parse_warning": "model returned non-JSON",
            "_raw": content[:1000],
            "_finish_reason": finish_reason,
        }
    obj = _normalise_timeline_map(obj)
    # A 27-token `{ "zeitgeist:": [] ... }` response is a provider/model
    # hiccup, not an editorial decision to publish nothing. Retry once with a
    # terse repair instruction, then use an honest extractive fallback.
    if not _timeline_map_has_coverage(obj):
        repair_messages = messages + [{"role": "user", "content": (
            "Your previous map selected nothing. Return valid JSON with 3-6 concrete items or stories "
            "from the supplied post IDs. Do not use keys ending in a colon."
        )}]
        retry_content, retry_usage, retry_model, retry_finish = _call(
            repair_messages, model=model or STAGE1_MODEL, max_tokens=STAGE1_MAX_TOKENS,
            max_reasoning_tokens=STAGE1_MAX_REASONING_TOKENS,
        )
        try:
            retry_obj = _normalise_timeline_map(parse_json_response(retry_content))
        except json.JSONDecodeError:
            retry_obj = {}
        usage = _combine_usage(usage, retry_usage)
        model_used, finish_reason = retry_model, retry_finish
        content = content + "\n\nRETRY:\n" + retry_content
        obj = retry_obj if _timeline_map_has_coverage(retry_obj) else _fallback_timeline_map(candidate_posts)
        if not _timeline_map_has_coverage(retry_obj):
            obj["_parse_warning"] = "stage 1 returned no usable coverage; extractive fallback used"

    # Also annotate if finish_reason itself is the problem (e.g. finish=error
    # with empty content — JSON parse would "succeed" against "" and give {})
    if finish_reason != "stop" and not obj.get("_parse_warning"):
        obj["_finish_reason"] = finish_reason
    # The caller persists this in a private local dossier, not the public
    # digest row. It is the difference between tuning a prompt from evidence
    # and guessing what the model actually returned.
    obj["_raw_output"] = content


    return obj, usage, model_used, finish_reason


def _normalise_timeline_map(obj):
    """Tolerate harmless JSON key typos, never malformed selection data."""
    if not isinstance(obj, dict):
        return {}
    out = {str(key).rstrip(":"): value for key, value in obj.items()}
    for key in ("zeitgeist", "stories_to_write", "items"):
        if not isinstance(out.get(key), list):
            out[key] = []
    out["editor_notes"] = str(out.get("editor_notes") or "")
    return out


def _timeline_map_has_coverage(obj):
    return bool((obj or {}).get("stories_to_write") or (obj or {}).get("items"))


def _fallback_timeline_map(posts):
    """Never leave a non-empty timeline with a blank digest after model failure."""
    def score(post):
        metrics = post.get("metrics") or {}
        return int(metrics.get("likes") or 0) + 2 * int(metrics.get("retweets") or 0) + int(metrics.get("replies") or 0)
    picks = sorted(posts, key=score, reverse=True)[:6]
    return {
        "zeitgeist": [], "stories_to_write": [],
        "items": [{"post_id": post["id"], "kind": "timeline", "why": "Selected extractively after editor failure."}
                  for post in picks],
        "editor_notes": "Source-first fallback.",
    }


def _combine_usage(first, second):
    out = dict(first or {})
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        out[key] = (first or {}).get(key, 0) + (second or {}).get(key, 0)
    return out


# -------- Stage 2: per-theme story --------

STAGE2_SYSTEM = """You are Herald's digest writer. You're writing an entry for a daily news digest,
synthesising a cluster of social-media posts into a coherent read. The upstream editor
(stage 1) has flagged this cluster as worth writing about and suggested a framing angle.
The editor also decides the scope — see SCOPE below.

SOURCE-GROUNDING RULES (HARD CONSTRAINTS):
1. Every factual claim MUST cite a source post inline as [post:ID], where ID is the post's `id`
   from the POSTS block. No bare assertions. A post reporting an event is a report or claim,
   not independent proof: write "X reported/argued/posted" when the supplied evidence does not
   establish the fact itself.
2. Direct quotes must be embedded verbatim with attribution: "quoted text" — @author.
3. Paraphrasing is fine. Fabrication is not. If the posts don't establish something, don't say it.
4. Don't repeat information already established by an earlier cited post — chain citations cleanly.
5. Do not make a broad story merely because posts share a subject. The supplied posts must have a
   concrete common event, argument, or cultural moment. Otherwise keep the entry narrow.
6. Do not add social proof such as "viral", "widely shared", "the timeline erupted", or a claim
   about what "the debate is really about" unless the supplied posts themselves establish it.
   Attribute contested framing to the person or posts making it; do not turn it into the narrator's
   verdict.

Output a JSON object (no markdown fences, no preamble) with keys:
  headline: short string ≤ 90 chars, captures the entry.
  story:    narrative prose with inline [post:ID] citations. Length scales with SCOPE (see below).
  topic:    short label (e.g. 'markets', 'ai', 'policy').
  key_quotes: array of {post_id, quote, speaker} — quotes you actually embedded in the story.
  coverage_type: 'one_shot' (default) or 'ongoing'.
  section: 'story' (always — this is the only section for digest entries).

VOICE & SCOPE:
- Herald is a digital-native editorial read of X — closer in tone to a sharp tech magazine
  (think Daring Fireball, The Verge's feature pages, Rest of World) than to a news brief or
  a casual chat. The reader is X-literate and time-poor; they want the read to feel like
  the timeline felt, not a sanitised summary of it.
- Match the energy of the source posts when it's distinctive. If the cluster is funny,
  write funny. If it's dry, write dry. If it's absurdist internet culture, name the
  absurdity — don't flatten it into "this matters because…" kickers.
- A single striking tweet can carry a whole story. Don't pad a one-post cluster with
  manufactured significance — if the post lands on its own, let it.
- A cluster of 8 posts all saying the same thing is still a one-paragraph story. Repetition
  isn't depth.
- Off-beat, sideways, weird, internet-native moments are welcome on their own merits. They
  don't need to be defended with "broader implications."
- Humour often gets lost in synthesis — models tend to flatten it into earnestness or
  news-ified framing. Lean toward the source's register rather than your own; if the
  posts are joking, the story should not be solemnly explaining the joke.
- The source-grounding rules above are HARD. The voice here is not — when in tension,
  grounding wins.

SCOPE (how much you write scales with how much source material you have):
- n_sources >= 5: full feature treatment, 3-5 paragraphs with embedded quotes and
  inline [post:ID] citations. Multiple accounts and angles welcome.
- n_sources 2-4: brief, 1-2 paragraphs. One clear angle, key quotes, citations.
- n_sources == 1: 1-2 sentences framing the tweet, OR pass it through verbatim with
  a [post:ID] citation if the tweet is already a complete thought. Don't pad a
  single post into a paragraph it doesn't need.
The user message tells you how many source posts you have. Trust the count and the
editor's framing angle. Don't manufacture depth the source material doesn't have.

EDITOR'S NOTES (from stage 1) may appear in the user message — these are the editor's
direction for today's tone and register. Follow them when they speak to your entry.
They are guidance, not rules — when in tension with the source-grounding rules, the
grounding rules win.

Context posts: an optional CONTEXT POSTS block may follow the POSTS. These are reply parents
or quote targets fetched via bird thread/read so the writer can see the conversation a reply
sits within, or what a quote-tweet is quote-tweeting. Cite context posts with the same
[post:ID] convention when a claim is grounded by them.

Image context (Phase 3.6.1): an IMAGE CONTEXT block follows the CONTEXT POSTS whenever the
cluster's posts include photos. Each line describes one attached photo in 1-2 sentences,
produced by a vision model that read the actual image bytes — so the descriptions are
grounded facts (specific chart values, headline text visible in the screenshot, what's
depicted in a meme, etc.). Use them as PRIMARY evidence in the story: when the post text
is short or vague (e.g. "Goddamn Patriot 🇺🇸" + a photo), the IMAGE CONTEXT block carries
the actual subject. Cite with [post:ID] when a claim relies on what's visible in the image,
the same way you'd cite a quote from CONTEXT POSTS. Per-story cap is configurable via
HERALD_IMAGES_PER_STORY (default 1). Videos and GIFs are intentionally skipped.

Link context (Phase 3.6.2): a LINK CONTEXT block follows the IMAGE CONTEXT whenever the
cluster's posts include outbound links. Each line shows the raw article body — HTML stripped
of nav/footer/scripts/ads/OG tags, no LLM in the middle. These are GROUNDED FACTS — quote
them or cite them with [post:ID] when they ground a claim. Per-story cap is configurable via
HERALD_LINKS_PER_STORY (default 2). The URL is shown so the writer can cite the source.
"""


# Per-story cap on image-context items rendered. Default 1; bump via env.
HERALD_IMAGES_PER_STORY = int(os.environ.get("HERALD_IMAGES_PER_STORY", "1"))

# Per-story cap on link-context items rendered. Phase 3.6.2 default is 2:
# text is denser than images, so two articles per story is useful without
# blowing the stage-2 token budget. Cap is on TOTAL link items across the
# cluster (not "N posts each contribute their best link").
HERALD_LINKS_PER_STORY = int(os.environ.get("HERALD_LINKS_PER_STORY", "2"))

# Per-link text truncation in the stage-2 prompt. The DB stores up to
# HERALD_LINK_MAX_CHARS_STORE (8000) for fidelity; we trim at render time
# so admins can tune the prompt cost without re-fetching.
HERALD_LINK_MAX_CHARS_RENDER = int(os.environ.get("HERALD_LINK_MAX_CHARS_RENDER", "4000"))


def stage2_story(theme_label, theme_posts, angle=None, timeline_map_summary=None,
                 editor_notes=None, model=None, dry_run=False, source_roles=None):
    """Stage 2: write a story for one theme.

    theme_label: the theme's label from the timeline map.
    theme_posts: list of post dicts (the posts that contributed to this theme).
    angle: 1-sentence framing suggested by the timeline map (optional).
    timeline_map_summary: small context block from the timeline map (optional).
    editor_notes: 1-3 sentences of editorial direction from stage 1 (optional).
                   Passed through to the user message as guidance for tone / register.

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
            "role": (source_roles or {}).get(str(p.get("id"))) or p.get("desk_role") or "source",
        })
    angle_line = f"\nFraming angle (from the timeline map): {angle}" if angle else ""
    context_line = f"\nTimeline context: {timeline_map_summary}" if timeline_map_summary else ""

    context_posts = _gather_context_for(theme_posts)
    context_block, _ = _render_context_block(context_posts)

    # Phase 3.6.1: pick up to HERALD_IMAGES_PER_STORY images from posts in
    # this theme that have _image_description set (set by
    # herald_media.attach_media_context). Selection is engagement-priority
    # so the most-liked/reposted post's image wins when a cluster has many.
    picked_images = _pick_images_for_story(theme_posts, HERALD_IMAGES_PER_STORY)
    image_block, n_imgs = _render_image_context_block(picked_images)
    if n_imgs:
        print(f"[synth] theme={theme_label!r} image_context={n_imgs}", file=sys.stderr)

    # Phase 3.6.2: pick up to HERALD_LINKS_PER_STORY link-context entries
    # from posts in this theme that have _link_context attached (set by
    # herald_links.attach_link_context). Cap is on total link items across
    # the cluster; engagement-priority sort surfaces the most-amplified
    # article when many links are present.
    picked_links = _pick_links_for_story(theme_posts, HERALD_LINKS_PER_STORY)
    link_block, n_links = _render_link_context_block(picked_links)
    if n_links:
        print(f"[synth] theme={theme_label!r} link_context={n_links}", file=sys.stderr)

    editor_notes_line = f"\nEditor's notes: {editor_notes}" if editor_notes else ""

    user_parts = [
        f"THEME: {theme_label}{angle_line}{context_line}",
        f"SCOPE: {len(posts_serialised)} source post(s).{editor_notes_line}",
        f"POSTS ({len(posts_serialised)}; each includes an editorial role):\n{json.dumps(posts_serialised, ensure_ascii=False)}",
    ]
    if context_block:
        user_parts.append(context_block)
    if image_block:
        user_parts.append(image_block)
    if link_block:
        user_parts.append(link_block)
    user_parts.append("Write the story JSON now.")
    user = "\n\n".join(user_parts)
    messages = [{"role": "system", "content": STAGE2_SYSTEM}, {"role": "user", "content": user}]
    content, usage, model_used, finish_reason = _call(
        messages, model=model or STAGE2_MODEL,
        max_reasoning_tokens=STAGE2_MAX_REASONING_TOKENS,
    )

    try:
        # Claude-family models may place literal newlines in the prose string.
        # They remain structurally valid for Herald once parsed non-strictly;
        # all citations and quotes are still checked below.
        obj = parse_json_response(content)
    except json.JSONDecodeError:
        obj = {
            "headline": f"Story on {theme_label}",
            "story": content,
            "topic": "unparsed",
            "key_quotes": [],
            "_parse_warning": "model returned non-JSON",
        }

    # Stage 2 can also return valid JSON with empty headline/story (saw this
    # on digest 24, story "World Cup controversy": {"headline": "", "story": ""}
    # with 1096 completion tokens — model tried but produced nothing usable).
    # setdefault() above only fills missing keys, not empty strings. Validate
    # here so storage and the web UI never see an empty story slot.
    if not obj.get("headline") or not obj.get("story"):
        obj["_parse_warning"] = obj.get("_parse_warning") or "model returned empty headline/story"
        obj["headline"] = obj.get("headline") or f"Story on {theme_label}"
        obj["story"] = obj.get("story") or f"[stage 2 produced no story body for theme '{theme_label}' — see pipeline_runs for finish_reason]"

    _citations_ok, bad_citations = _validate_story_citations(obj.get("story"), theme_posts)
    if bad_citations:
        # A citation-looking string is worse than no prose: it gives a
        # fabricated assertion the appearance of sourcing. Preserve raw model
        # output in the dossier, but fall back to the selected source itself.
        first = theme_posts[0] if theme_posts else {}
        obj["_citation_rejections"] = bad_citations
        obj["story"] = f"{first.get('text') or ''} [post:{first.get('id') or ''}]"

    obj.setdefault("topic", "")
    valid_quotes, rejected_quotes = _validate_key_quotes(obj.get("key_quotes"), theme_posts)
    obj["key_quotes"] = valid_quotes
    if rejected_quotes:
        # Stored in the private dossier by herald_digest, never rendered.
        obj["_quote_rejections"] = rejected_quotes
    obj.setdefault("coverage_type", "one_shot")
    obj["section"] = "story"
    obj["theme_label"] = theme_label
    obj["angle"] = angle  # the framing angle suggested by stage 1 (may be None)
    obj["model_version"] = model_used
    obj["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    obj["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    obj["cost_usd"] = float(usage.get("cost") or 0.0)
    obj["finish_reason"] = finish_reason
    obj["_raw_output"] = content
    return obj, usage, model_used, finish_reason


def _normalise_quote_text(value):
    return " ".join(str(value or "").split()).strip(" \t\n\"“”'")


def _validate_key_quotes(quotes, theme_posts):
    """Keep only actual verbatim source quotes; never render model glitches."""
    sources = {}
    for post in theme_posts:
        if post.get("id"):
            sources[str(post["id"])] = post.get("text") or ""
    for context in _gather_context_for(theme_posts):
        if context.get("id"):
            sources[str(context["id"])] = context.get("text") or ""

    valid, rejected = [], []
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        post_id = str(quote.get("post_id") or "")
        candidate = _normalise_quote_text(quote.get("quote"))
        source = _normalise_quote_text(sources.get(post_id))
        if len(candidate) >= 8 and candidate in source:
            valid.append(quote)
        else:
            rejected.append({"post_id": post_id, "quote": str(quote.get("quote") or "")[:180]})
    return valid, rejected


_CITATION_RE = re.compile(r"\[post:([^\]]+)\]")


def _validate_story_citations(story, theme_posts):
    """Ensure every citation is an exact supplied post/context ID."""
    allowed = {str(post.get("id")) for post in theme_posts if post.get("id")}
    allowed.update(str(post.get("id")) for post in _gather_context_for(theme_posts) if post.get("id"))
    bad = []
    for raw in _CITATION_RE.findall(story or ""):
        if not raw.isdigit() or raw not in allowed:
            bad.append(raw[:120])
    return not bad, bad

# ---------- shared helpers ----------

def _post_engagement(post, *, retweet_weight=3, reply_weight=0):
    """Return a scalar engagement score for a post.

    Used by image/link pickers (which want amplification: likes + 3*retweets).
    herald_embed and herald_digest use the same metric dict with different
    weights (2*retweets + replies) for the ranker; see `_RANKER_ENGAGEMENT`
    there. Centralising the metric parsing here so the snake_case / camelCase
    fallback (`likes` vs `like_count`) lives in one place.
    """
    m = (post or {}).get("metrics") or {}
    likes = int(m.get("likes") or m.get("like_count") or 0)
    rts = int(m.get("retweets") or m.get("retweet_count") or 0)
    replies = int(m.get("replies") or m.get("reply_count") or 0)
    return likes + rts * retweet_weight + replies * reply_weight

# ---------- image context rendering (Phase 3.6.1) ----------

def _pick_images_for_story(theme_posts, max_images):
    """Pick up to `max_images` photos from a story's theme posts.

    Selection rule (deterministic, no LLM):
      1. Posts in the theme cluster that have _image_description set.
      2. Within each, prefer highest engagement (likes + retweets*3) as a
         proxy for "the post people engaged with most".
      3. Tie-break by earlier appearance in theme_posts (stable order).

    Returns list of (post_id, author, description) tuples, ordered.

    If max_images=0 (HERALD_IMAGES_PER_STORY=0 disables image context),
    returns []. If max_images=1 (default), at most one image renders per
    story — per James, 2026-07-05.
    """
    if max_images <= 0:
        return []
    eligible = []
    for p in theme_posts:
        desc = p.get("_image_description")
        if not desc:
            continue
        score = _post_engagement(p)
        eligible.append((score, p))
    # Sort by score desc, then index in theme_posts (stable).
    eligible.sort(key=lambda x: (-x[0],))
    out = []
    for _, p in eligible[:max_images]:
        author = (p.get("author") or {}).get("username", "unknown")
        out.append((p.get("id"), author, p["_image_description"]))
    return out


def _render_image_context_block(picked_images, max_chars=400):
    """Render the IMAGE CONTEXT block for the stage 2 prompt.

    Format mirrors _render_context_block — one line per image, prefixed
    so the LLM clearly distinguishes image-derived facts from quote-derived
    facts. We don't show the URL itself (the LLM doesn't need it; the
    description carries the meaning).

    Returns: (block_text, n_images) or ("", 0) when none.
    """
    if not picked_images:
        return "", 0
    lines = [
        f"IMAGE CONTEXT ({len(picked_images)} parsed; each shows a vision-model "
        f"description of one attached photo. These are GROUNDED FACTS — the "
        f"vision model read the actual image bytes, so specific chart values, "
        f"headline text visible in a screenshot, and what's depicted in a meme "
        f"are accurate. Use as PRIMARY evidence when the post text is short or "
        f"vague. Cite with [post:ID]):"
    ]
    for pid, author, desc in picked_images:
        text = (desc or "").replace("\n", " ")[:max_chars]
        lines.append(f"  🖼  [post:{pid}] @{author}: {text}")
    return "\n".join(lines), len(picked_images)


# ---------- link context rendering (Phase 3.6.2) ----------

def _pick_links_for_story(theme_posts, max_links):
    """Pick up to `max_links` link-context entries from a story's theme posts.

    Selection rule (deterministic, no LLM):
      1. Posts in the theme cluster that have _link_context attached
         (set by herald_links.attach_link_context). Each post may carry
         multiple links.
      2. Within the union, prefer highest engagement (likes + retweets*3)
         — same proxy as _pick_images_for_story so the most-amplified
         article surfaces first when a cluster has many links.
      3. Tie-break by earlier appearance in theme_posts, then by the link's
         position within that post (stable order).

    Returns list of (post_id, url, text) tuples, ordered by score desc.

    Cap is on total link items across the cluster (not "N posts each
    contribute their best link") so a single high-engagement post that
    linked to three articles can fill the cap on its own.

    If max_links=0 (HERALD_LINKS_PER_STORY=0 disables link context),
    returns []. If max_links=2 (default), at most two articles render per
    story.
    """
    if max_links <= 0:
        return []
    eligible = []
    for idx, p in enumerate(theme_posts):
        entries = p.get("_link_context") or []
        if not entries:
            continue
        score = _post_engagement(p)
        for entry_idx, entry in enumerate(entries):
            eligible.append((score, idx, entry_idx, p.get("id"), entry.get("url"), entry.get("text")))
    # Stable sort: score desc, then post order, then link order within post.
    eligible.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [(pid, url, text) for (_, _, _, pid, url, text) in eligible[:max_links]]


def _render_link_context_block(picked_links, max_chars=None):
    """Render the LINK CONTEXT block for the stage 2 prompt.

    Format mirrors _render_image_context_block — one line per link,
    prefixed so the LLM clearly distinguishes link-derived facts from
    image-derived facts. The URL is shown (unlike images) so the LLM
    has a citation surface when it picks one out of the block.

    Returns: (block_text, n_links) or ("", 0) when none.
    """
    if not picked_links:
        return "", 0
    if max_chars is None:
        max_chars = HERALD_LINK_MAX_CHARS_RENDER
    header = (
        f"LINK CONTEXT ({len(picked_links)} parsed; each shows the raw article "
        f"body — HTML stripped of nav/footer/scripts/ads/OG tags, fetched by "
        f"HTTP GET with no LLM in the middle. These are GROUNDED FACTS — quote "
        f"them or cite them with [post:ID] when they ground a claim.):"
    )
    lines = [header]
    for pid, url, text in picked_links:
        body = (text or "").replace("\n", " ")
        truncated = body[:max_chars]
        suffix = ""
        if len(body) > max_chars:
            suffix = " …[+truncated]"
        lines.append(f"  📰 [post:{pid}] {url}: {truncated}{suffix}")
    return "\n".join(lines), len(picked_links)
