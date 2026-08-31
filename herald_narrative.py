"""Narrative v2: a bounded editor -> evidence -> whole-issue writer pipeline.

Collection is deliberately absent from this module.  It works from ordered
timeline snapshots, spends X reads only after editorial selection, and makes
two bounded LLM calls per issue (plan and write), never one call per story.
"""
import json
import os
import re

import herald_edition
import herald_db
import herald_embed
import herald_links
import herald_synthesise


MAX_CANDIDATES = int(os.environ.get("HERALD_NARRATIVE_CANDIDATES", "48"))
MAX_STORIES = int(os.environ.get("HERALD_NARRATIVE_STORIES", "7"))
MAX_POSTS_PER_STORY = int(os.environ.get("HERALD_NARRATIVE_POSTS_PER_STORY", "7"))
MAX_CONTEXT_READS = int(os.environ.get("HERALD_NARRATIVE_CONTEXT_READS", "12"))
MAX_LINK_FETCHES = int(os.environ.get("HERALD_NARRATIVE_LINK_FETCHES", "8"))
PLANNER_MAX_TOKENS = int(os.environ.get("HERALD_NARRATIVE_PLANNER_MAX_TOKENS", "2200"))
PLANNER_REASONING_TOKENS = int(os.environ.get("HERALD_NARRATIVE_PLANNER_REASONING_TOKENS", "600"))
WRITER_MAX_TOKENS = int(os.environ.get("HERALD_NARRATIVE_WRITER_MAX_TOKENS", "5200"))
WRITER_REASONING_TOKENS = int(os.environ.get("HERALD_NARRATIVE_WRITER_REASONING_TOKENS", "1000"))


def _timeline_shape(post):
    """Add the parser-shaped author block expected by embed/synthesis helpers."""
    post = dict(post)
    post["author"] = {"id": post.get("author_id"), "username": post.get("author_handle", "")}
    post["id"] = str(post.get("external_id") or post.get("id"))
    return post


def _candidate_posts(posts, dry_run):
    shaped = [_timeline_shape(p) for p in posts]
    if dry_run:
        return shaped[:MAX_CANDIDATES], {"prompt_tokens": 0, "cost": 0.0}
    indices, _clusters, usage = herald_embed.select_candidates(shaped, vectors=None, max_candidates=MAX_CANDIDATES)
    return [shaped[i] for i in indices], usage


def _fallback_plan(candidates):
    """A usable extractive fallback when the planning call is unavailable."""
    groups = []
    for p in candidates[:MAX_STORIES]:
        groups.append({"label": "timeline pick", "angle": "A notable moment from the timeline.",
                       "post_ids": [p["id"]], "kind": "sidebar", "research": False})
    return {"stories": groups, "editor_note": "Keep the source's own register."}


def _plan_issue(candidates, dry_run=False):
    if dry_run:
        return _fallback_plan(candidates), {}, "dry-run", "stop"
    serialised = [{"id": p["id"], "author": p["author"]["username"], "text": (p.get("text") or "")[:1000],
                   "metrics": p.get("metrics") or {}, "type": p.get("post_type"), "position": i}
                  for i, p in enumerate(candidates)]
    system = f"""You are Herald's senior editor. Turn a personal X timeline into a finite,
story-based issue, not a list of tweets and not a conventional newspaper. Pick news, arguments,
jokes, sports, cultural moments, technical posts, or oddities when they genuinely earn a place.
Return JSON only: {{"editor_note":string,"stories":[{{"label":string,"angle":string,
"kind":"news|argument|culture|sidebar","post_ids":[string],"research":boolean}}]}}.
Choose 3-{MAX_STORIES} stories and at most {MAX_POSTS_PER_STORY} supplied posts per story.
Use only supplied IDs. `research` is true only for a concrete event where external grounding
would clarify what happened. Do not write prose or claim facts in the plan.
CRITICAL: do not force unrelated posts together. A news, argument, or culture story needs at
least two posts that demonstrably concern the same subject. A one-post selection is allowed
only as kind=sidebar, and there should be no more than two sidebars."""
    content, usage, model, finish = herald_synthesise._call(
        [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(serialised, ensure_ascii=False)}],
        max_tokens=PLANNER_MAX_TOKENS, max_reasoning_tokens=PLANNER_REASONING_TOKENS,
    )
    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        return _fallback_plan(candidates), usage, model, finish
    known = {p["id"] for p in candidates}
    stories = []
    for item in plan.get("stories") or []:
        ids = [str(pid) for pid in item.get("post_ids") or [] if str(pid) in known][:MAX_POSTS_PER_STORY]
        kind = str(item.get("kind") or "sidebar")
        if len(ids) == 1 and kind != "sidebar":
            continue
        if ids and isinstance(item.get("label"), str):
            stories.append({"label": item["label"][:100], "angle": str(item.get("angle") or "")[:280],
                            "kind": kind, "post_ids": ids,
                            "research": bool(item.get("research"))})
    if not stories:
        return _fallback_plan(candidates), usage, model, finish
    return {"stories": stories[:MAX_STORIES], "editor_note": str(plan.get("editor_note") or "")[:500]}, usage, model, finish


def _collect_evidence(plan, by_id, dry_run=False):
    """Attach only selected reply/quote parents and linked articles."""
    selected = [by_id[pid] for story in plan["stories"] for pid in story["post_ids"] if pid in by_id]
    seen = set()
    selected = [p for p in selected if not (p["id"] in seen or seen.add(p["id"]))]
    budget = [MAX_CONTEXT_READS]
    if not dry_run:
        for post in selected:
            if herald_edition._opaque(post):
                post["_narrative_context"] = herald_edition._fetch_context(post, budget)
        # Article context improves a selected story but must never make the
        # whole issue unavailable (SQLite cache contention, paywalls, and
        # malformed pages are all expected operational failures).
        try:
            herald_links.enrich_link_batch(selected, max_fetches=MAX_LINK_FETCHES)
            herald_links.attach_link_context(selected)
        except Exception:
            for post in selected:
                post.setdefault("_link_context", [])
    return selected, {"context_reads": MAX_CONTEXT_READS - budget[0] if not dry_run else 0}


def _evidence_prompt(plan, by_id):
    blocks = []
    allowed_ids = set()
    for n, story in enumerate(plan["stories"], 1):
        lines = [f"STORY {n}: {story['label']}\nANGLE: {story['angle']}\nKIND: {story['kind']}\nPOSTS:"]
        for pid in story["post_ids"]:
            p = by_id[pid]
            allowed_ids.add(pid)
            lines.append(f"[post:{pid}] @{p['author']['username']}: {(p.get('text') or '')[:1400]}")
            for context in p.get("_narrative_context") or []:
                if context.get("id"):
                    allowed_ids.add(str(context["id"]))
                    lines.append(f"CONTEXT [post:{context['id']}] @{context.get('author','')}: {(context.get('text') or '')[:900]}")
            for link in (p.get("_link_context") or [])[:1]:
                lines.append(f"ARTICLE CONTEXT FROM [post:{pid}] {link['url']}: {link['text'][:2500]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), allowed_ids


def _write_issue(plan, by_id, dry_run=False):
    evidence, allowed_ids = _evidence_prompt(plan, by_id)
    if dry_run:
        return _extractive_fallback(plan, by_id), {}, "dry-run", "stop"
    system = """You write Herald, a 10-15 minute editorial read of a personal X timeline.
Write a coherent issue from the supplied evidence packs. It should feel like the timeline felt:
sharp, curious, funny when appropriate, and willing to put a news event beside a meme or
argument. Do not flatten everything into hard news.

Facts need [post:ID] citations. ARTICLE CONTEXT is evidence attached to the post that linked it:
cite that post ID when using it. Do not add facts not established by supplied posts or article
context. Distinguish what happened from how the timeline reacted; never turn reaction into fact.
Output JSON only: {"stories":[{"story_index":integer,"headline":string,"story":string,"topic":string,
"key_quotes":[{"post_id":string,"quote":string,"speaker":string}]}]}. Write one entry for
each evidence pack, using its exact STORY number as story_index. Total issue target: 1,800-2,500 words. Use 1-4
paragraphs per entry according to evidence depth; a joke/sidebar may be brief."""
    content, usage, model, finish = herald_synthesise._call(
        [{"role": "system", "content": system}, {"role": "user", "content": evidence}],
        max_tokens=WRITER_MAX_TOKENS, max_reasoning_tokens=WRITER_REASONING_TOKENS,
    )
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return [], usage, model, finish
    planned = {i: s for i, s in enumerate(plan["stories"], 1)}
    stories = []
    for item in result.get("stories") or []:
        try:
            story_index = int(item.get("story_index"))
        except (TypeError, ValueError):
            continue
        source = planned.get(story_index)
        text = str(item.get("story") or "")
        cited = set(re.findall(r"\[post:([^\]]+)\]", text))
        if not source or not text or not cited or not cited.issubset(allowed_ids):
            continue
        stories.append({"headline": str(item.get("headline") or source["label"])[:120], "story": text,
                        "topic": str(item.get("topic") or source["kind"]), "key_quotes": item.get("key_quotes") or [],
                        "post_ids": source["post_ids"], "theme_label": source["label"], "angle": source["angle"],
                        "section": "story", "coverage_type": "one_shot"})
    return stories, usage, model, finish


def _extractive_fallback(plan, by_id):
    """Publish source text rather than an empty issue if the writer fails."""
    stories = []
    for item in plan["stories"]:
        p = by_id[item["post_ids"][0]]
        stories.append({"headline": item["label"], "story": f"{p.get('text') or ''} [post:{p['id']}]",
                        "topic": item["kind"], "key_quotes": [], "post_ids": item["post_ids"],
                        "theme_label": item["label"], "angle": item["angle"], "section": "story",
                        "coverage_type": "one_shot"})
    return stories


def build_issue(conn, since_ts, until_ts, dry_run=False):
    posts, captured = herald_db.get_timeline_window(conn, since_ts, until_ts)
    if not posts:
        return None
    candidates, embed_usage = _candidate_posts(posts, dry_run)
    plan, plan_usage, plan_model, plan_finish = _plan_issue(candidates, dry_run)
    by_id = {p["id"]: p for p in candidates}
    _selected, evidence_stats = _collect_evidence(plan, by_id, dry_run)
    stories, writer_usage, writer_model, writer_finish = _write_issue(plan, by_id, dry_run)
    if not stories:
        stories = _extractive_fallback(plan, by_id)
    return {"stories": stories, "plan": plan, "captured": captured, "candidate_count": len(candidates),
            "candidate_ids": [p["id"] for p in candidates],
            "evidence": evidence_stats, "models": [plan_model, writer_model],
            "usage": {"prompt_tokens": int(embed_usage.get("prompt_tokens") or 0) + int(plan_usage.get("prompt_tokens") or 0) + int(writer_usage.get("prompt_tokens") or 0),
                      "completion_tokens": int(plan_usage.get("completion_tokens") or 0) + int(writer_usage.get("completion_tokens") or 0),
                      "cost": float(embed_usage.get("cost") or 0) + float(plan_usage.get("cost") or 0) + float(writer_usage.get("cost") or 0)},
            "finish": {"plan": plan_finish, "writer": writer_finish}}
