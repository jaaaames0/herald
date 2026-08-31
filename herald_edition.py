"""Bounded, timeline-first edition assembly.

This module deliberately does not fetch X during collection.  Its only X
context calls happen after a post has cleared a small selection threshold.
The normal output is extractive cards; the model is an editor, not a reporter.
"""
import json
import os
import time
import urllib.request

import herald_context
import herald_db


EDITOR_MODEL = os.environ.get("HERALD_EDITOR_MODEL", os.environ.get("HERALD_MODEL", "openai/gpt-oss-120b"))
RESEARCH_MODEL = os.environ.get("HERALD_RESEARCH_MODEL", "perplexity/sonar")
MAX_EDITOR_CANDIDATES = int(os.environ.get("HERALD_EDITION_CANDIDATES", "80"))
MAX_CARDS = int(os.environ.get("HERALD_EDITION_CARDS", "22"))
PREFETCH_READS = 3
MAX_CONTEXT_READS = 12
EDITOR_MAX_TOKENS = int(os.environ.get("HERALD_EDITOR_MAX_TOKENS", "1500"))
EDITOR_MAX_REASONING_TOKENS = int(os.environ.get("HERALD_EDITOR_MAX_REASONING_TOKENS", "500"))
RESEARCH_MAX_TOKENS = int(os.environ.get("HERALD_RESEARCH_MAX_TOKENS", "3000"))
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _engagement(post):
    m = post.get("metrics") or {}
    try:
        return int(m.get("likes") or 0) + 2 * int(m.get("retweets") or 0) + int(m.get("replies") or 0)
    except (TypeError, ValueError):
        return 0


def _opaque(post):
    return post.get("post_type") in ("reply", "quote") and len((post.get("text") or "").strip()) < 180


def preliminary_candidates(posts):
    """Keep source order, with a small engagement lift rather than hard ranking."""
    recent = posts[:MAX_EDITOR_CANDIDATES]
    # A few unusually amplified posts outside the first page remain eligible.
    lift = sorted(posts[MAX_EDITOR_CANDIDATES:], key=_engagement, reverse=True)[:10]
    seen = set()
    return [p for p in recent + lift if not (p["id"] in seen or seen.add(p["id"]))]


def _fetch_context(post, budget):
    """Resolve one missing parent/quote target. Never recursively traverses a thread."""
    if budget[0] <= 0:
        return []
    target = None
    kind = None
    if post.get("quoted_tweet_id"):
        target, kind = post["quoted_tweet_id"], "quote"
    elif post.get("in_reply_to_id"):
        target, kind = post["in_reply_to_id"], "reply"
    if not target:
        return []
    budget[0] -= 1
    try:
        raw = herald_context.fetch_parent_raw(target)
        parsed = herald_context.herald_parser.parse_tweet(raw)
        return [{"id": parsed.get("id"), "kind": kind, "text": parsed.get("text"),
                 "author": (parsed.get("author") or {}).get("username", "")}]
    except Exception:
        return []


def _editor_manifest(candidates, dry_run=False):
    """Ask for selection metadata only; fallback is deterministic and extractive."""
    fallback = {
        "clusters": [],
        "front_ids": [p["id"] for p in candidates[:4]],
        "card_ids": [p["id"] for p in candidates[:MAX_CARDS]],
    }
    if dry_run or not os.environ.get("OPENROUTER_API_KEY"):
        return fallback, {}
    payload_posts = [{"id": p["id"], "author": p.get("author_handle"), "text": (p.get("text") or "")[:800],
                      "position": i, "type": p.get("post_type")} for i, p in enumerate(candidates)]
    system = """You edit a finite personal X timeline edition. Select, do not report.
Return JSON only: {clusters:[{label,post_ids,note}],front_ids:[id],card_ids:[id]}.
Use only supplied IDs. At most 4 clusters, 4 front_ids and 22 card_ids. Keep cards
close to source order. Labels and notes are descriptive, not factual claims."""
    data = {"model": EDITOR_MODEL, "response_format": {"type": "json_object"},
            "max_tokens": EDITOR_MAX_TOKENS, "max_reasoning_tokens": EDITOR_MAX_REASONING_TOKENS,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(payload_posts, ensure_ascii=False)}]}
    try:
        req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(data).encode(), headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.loads(response.read().decode())
        obj = json.loads(result["choices"][0]["message"]["content"])
        known = {str(p["id"]): p["id"] for p in candidates}
        for key in ("front_ids", "card_ids"):
            obj[key] = [known[str(x)] for x in obj.get(key, []) if str(x) in known]
        obj["card_ids"] = obj["card_ids"][:MAX_CARDS]
        obj["front_ids"] = obj["front_ids"][:4]
        clusters = []
        for c in obj.get("clusters", [])[:4]:
            ids = [known[str(x)] for x in c.get("post_ids", []) if str(x) in known]
            if ids and isinstance(c.get("label"), str):
                clusters.append({"label": c["label"][:100], "post_ids": ids, "note": str(c.get("note") or "")[:240]})
        obj["clusters"] = clusters
        return obj, result.get("usage") or {}
    except Exception:
        return fallback, {}


def _research(lead, dry_run=False):
    """Return a separately-cited research brief, or None on any weak result."""
    if dry_run or not lead or not os.environ.get("OPENROUTER_API_KEY"):
        return None, {}
    prompt = """Research this concrete topic from an X timeline. Return JSON only with
lead_label, body, sources. body must use [source:1], [source:2] etc after every factual
sentence. sources must contain at least two independent web sources with url,title,excerpt.
If this is opinion, humour, or cannot be corroborated, return {\"status\":\"unavailable\"}.
Do not cite X as factual evidence. Topic: """ + (lead.get("label") or lead.get("text") or "")
    data = {"model": RESEARCH_MODEL, "response_format": {"type": "json_object"},
            "max_tokens": RESEARCH_MAX_TOKENS,
            "tools": [{"type": "openrouter:web_search"}],
            "messages": [{"role": "user", "content": prompt}]}
    try:
        req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(data).encode(), headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode())
        obj = json.loads(result["choices"][0]["message"]["content"])
        sources = [s for s in obj.get("sources", []) if s.get("url")][:8]
        if obj.get("status") == "unavailable" or len(sources) < 2 or not obj.get("body"):
            return None, result.get("usage") or {}
        for i in range(1, len(sources) + 1):
            if f"[source:{i}]" not in obj["body"]:
                return None, result.get("usage") or {}
        return ({"lead_label": str(obj.get("lead_label") or lead.get("label") or "Lead topic"),
                 "body": obj["body"], "status": "success", "sources": sources},
                result.get("usage") or {})
    except Exception:
        return None, {}


def build_edition(conn, since_ts, until_ts, dry_run=False):
    posts, captured_rows = herald_db.get_timeline_window(conn, since_ts, until_ts)
    if not posts:
        return None
    candidates = preliminary_candidates(posts)
    budget = [MAX_CONTEXT_READS]
    # Small prefetch only for opaque, otherwise likely-to-be-visible posts.
    for post in sorted([p for p in candidates if _opaque(p)], key=_engagement, reverse=True)[:PREFETCH_READS]:
        post["_edition_context"] = _fetch_context(post, budget)
    manifest, editor_usage = _editor_manifest(candidates, dry_run=dry_run)
    by_id = {p["id"]: p for p in posts}
    cards = [by_id[pid] for pid in manifest["card_ids"] if pid in by_id]
    # Spend remaining reads only on cards that will render.
    for card in cards:
        if _opaque(card) and not card.get("_edition_context"):
            card["_edition_context"] = _fetch_context(card, budget)
    for card in cards:
        card["section"] = "front" if card["id"] in manifest["front_ids"] else "timeline"
        card["context_post_ids"] = [x["id"] for x in card.get("_edition_context") or [] if x.get("id")]
    clusters = manifest["clusters"]
    lead = clusters[0] if clusters else (cards[0] if cards else None)
    research, research_usage = _research(lead, dry_run=dry_run)
    ledger = {"captured": captured_rows, "unique": len(posts), "candidate_pool": len(candidates),
              "shown": len(cards), "collapsed_or_omitted": max(0, len(posts) - len(cards)),
              "context_reads": MAX_CONTEXT_READS - budget[0], "research": "available" if research else "unavailable"}
    return {"cards": cards, "clusters": clusters, "ledger": ledger, "research": research,
            "usage": {"prompt_tokens": int(editor_usage.get("prompt_tokens") or 0) + int(research_usage.get("prompt_tokens") or 0),
                      "completion_tokens": int(editor_usage.get("completion_tokens") or 0) + int(research_usage.get("completion_tokens") or 0),
                      "cost": float(editor_usage.get("cost") or 0) + float(research_usage.get("cost") or 0)}}
