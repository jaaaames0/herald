"""Persistent, on-demand editorial story desk for Herald.

This is intentionally not the legacy digest planner. It reviews a wider slice
of the captured timeline and returns *anchored candidates* that can stay in
``watching`` until enough evidence exists. It does not write or publish prose.
"""
import json
import os
import re

import herald_db
import herald_evidence
import herald_links
import herald_synthesise


MAX_POSTS = int(os.environ.get("HERALD_DESK_MAX_POSTS", "150"))
# Desk experiments should not require changing the model used by the legacy
# digest planner. The default deliberately inherits the editor model.
DESK_MODEL = os.environ.get("HERALD_DESK_MODEL", herald_synthesise.STAGE1_MODEL)
DESK_CONTEXT_READS = int(os.environ.get("HERALD_DESK_EVIDENCE_CONTEXT_READS", "8"))
DESK_LINK_FETCHES = int(os.environ.get("HERALD_DESK_EVIDENCE_LINK_FETCHES", "4"))
DESK_THREAD_FETCHES = int(os.environ.get("HERALD_DESK_EVIDENCE_THREAD_FETCHES", "1"))
DESK_WRITER_MODEL = os.environ.get("HERALD_DESK_WRITER_MODEL", herald_synthesise.STAGE2_MODEL)
VALID_TYPES = {"reported_event", "timeline_argument", "culture", "one_shot"}
VALID_STATES = {"watching", "ready"}
VALID_URGENCY = {"breaking", "developing", "normal"}
ANCHOR_STOPWORDS = frozenset({
    "about", "agentic", "claims", "coding", "concrete", "decades", "debate", "event",
    "from", "have", "including", "into", "models", "moment", "more", "news", "over",
    "reported", "story", "that", "their", "these", "this", "timeline", "tools", "using",
    "with", "would", "years",
})

SYSTEM = """You are Herald's story desk. Your job is NOT to write a digest and
NOT to group posts by broad subject. Find the distinct stories, arguments, and
cultural moments that may deserve evidence gathering over time.

Return JSON only:
{
  "candidates": [
    {
      "lead_post_id": "one input ID",
      "anchor": "specific event, claim, question, or cultural moment",
      "story_type": "reported_event|timeline_argument|culture|one_shot",
      "state": "watching|ready",
      "urgency": "breaking|developing|normal",
      "readiness": 0.0,
      "continues_candidate_id": "optional ID from STORY LEDGER, or null",
      "continuity_note": "optional, concrete description of what has changed",
      "rationale": "why this is coherent and whether it needs more evidence",
      "sources": [
        {"post_id":"input ID", "role":"lead|reaction|counterpoint|colour", "relevance":0.0}
      ]
    }
  ]
}

Rules:
- An anchor must be concrete. “AI developments”, “geopolitics”, and “Bitcoin
  buzz” are invalid anchors.
- Every listed source must directly concern the same anchor. Do not join posts
  merely because they share a sector or mood.
- A lead must itself establish the anchor. Never label an unrelated joke,
  image-only post, or vague reaction as a lead because it happened to be
  highly engaged.
- A self-contained notable post may be a one_shot. Do not make up a trend for it.
- Choose watching when the topic is promising but needs a quoted parent, a
  counterpoint, a linked source, or more timeline reaction. Choose ready only
  when the supplied sources already support an honest, self-contained story.
- Breaking means it is time-sensitive and has enough evidence now. Developing
  means it is likely to benefit from accumulating material. Normal is neither.
- Timeline arguments are welcome: identify the proposition and genuine response,
  not generic commentary.
- Use only input post IDs. Return at most 8 candidates. Omit weak material.
- STORY LEDGER is Herald's already-known work. Set continues_candidate_id only
  when this is the same concrete event, argument, or cultural moment as that
  ledger entry. Shared sector, company, or broad subject is not enough.
- A continuation must add a meaningful development, reaction, or evidence. Do
  not repackage an old story merely because it appears again in the timeline.
- The ledger is incomplete by design. Do not imply that omitted prior material
  does not exist, and do not invent historical context.
"""


def _score(post):
    metrics = post.get("metrics") or {}
    return (int(metrics.get("likes") or 0) + 2 * int(metrics.get("retweets") or 0)
            + int(metrics.get("replies") or 0))


def _serialise(posts):
    return [{
        "id": str(p.get("id")),
        "author": (p.get("author") or {}).get("username", p.get("author_handle", "unknown")),
        "text": (p.get("text") or "")[:650],
        "created_at": p.get("created_at"),
        "metrics": p.get("metrics") or {},
        "post_type": p.get("post_type"),
        "quoted_tweet_id": p.get("quoted_tweet_id") or (p.get("quoted_tweet") or {}).get("id"),
    } for p in posts]


def _anchor_terms(text):
    return {token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(token) >= 4 and token not in ANCHOR_STOPWORDS}


def _normalise(raw, valid_ids, post_texts=None, ledger_ids=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
        return []
    out = []
    for item in raw["candidates"][:8]:
        if not isinstance(item, dict):
            continue
        lead = str(item.get("lead_post_id") or "")
        anchor = " ".join(str(item.get("anchor") or "").split())
        sources = item.get("sources") or []
        if lead not in valid_ids or not anchor or not isinstance(sources, list):
            continue
        roles, seen = [], set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            post_id = str(source.get("post_id") or "")
            if post_id not in valid_ids or post_id in seen:
                continue
            seen.add(post_id)
            role = str(source.get("role") or "source").lower()
            if role not in {"lead", "reaction", "counterpoint", "colour", "source"}:
                role = "source"
            try:
                relevance = max(0.0, min(1.0, float(source.get("relevance", 1))))
            except (TypeError, ValueError):
                relevance = 1.0
            roles.append((post_id, role, relevance))
        if lead not in seen:
            roles.insert(0, (lead, "lead", 1.0))
        # A lead is a factual/editorial foundation, not merely a card the
        # planner happened to associate. Require lexical evidence of the
        # concrete anchor for leads only; reactions may be terse and indirect.
        # This catches obvious high-engagement image/meme contamination without
        # turning the rule into a broad relevance filter.
        if post_texts:
            anchor_terms = _anchor_terms(anchor)
            filtered = []
            for post_id, role, relevance in roles:
                source_terms = _anchor_terms(post_texts.get(post_id, ""))
                if role == "lead" and anchor_terms and not (anchor_terms & source_terms):
                    continue
                filtered.append((post_id, role, relevance))
            roles = filtered
            valid_leads = [entry for entry in roles if entry[1] == "lead"]
            if not valid_leads:
                continue
            lead = valid_leads[0][0]
        if not roles:
            continue
        story_type = item.get("story_type") if item.get("story_type") in VALID_TYPES else "timeline_argument"
        state = item.get("state") if item.get("state") in VALID_STATES else "watching"
        urgency = item.get("urgency") if item.get("urgency") in VALID_URGENCY else "normal"
        try:
            readiness = max(0.0, min(1.0, float(item.get("readiness", 0))))
        except (TypeError, ValueError):
            readiness = 0.0
        try:
            continuation = int(item.get("continues_candidate_id"))
        except (TypeError, ValueError):
            continuation = None
        if continuation not in (ledger_ids or set()):
            continuation = None
        # Lead-post identity is stable across desk reviews, unlike LLM wording.
        out.append({
            "anchor_key": f"lead:{lead}", "anchor": anchor, "story_type": story_type,
            "state": state, "urgency": urgency, "readiness": readiness,
            "rationale": str(item.get("rationale") or "")[:600], "sources": roles,
            "continuity_candidate_id": continuation,
            "continuity_note": str(item.get("continuity_note") or "")[:400],
        })
    return out


def _parse_response(raw_text):
    """Accept JSON objects, including the common harmless ```json wrapper."""
    text = (raw_text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        return {}


def _ledger_for_prompt(conn, limit=30):
    """A compact, local-only record of prior desk work for continuity choices.

    This deliberately searches neither X nor the web. A candidate can benefit
    from history that Herald actually captured, but an empty/young archive is
    a normal state rather than a reason to stall the desk.
    """
    rows = herald_db.list_story_candidates(conn, limit=limit)
    return [{
        "id": row["id"], "anchor": row["anchor"], "state": row["state"],
        "story_type": row["story_type"], "source_count": row["source_count"],
        "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        "rationale": (row.get("rationale") or "")[:280],
        "continuity_parent_id": row.get("continuity_parent_id"),
    } for row in rows]


def review(conn, since_ts, until_ts, dry_run=False):
    """Review timeline material and persist source-backed desk candidates."""
    posts = herald_db.get_posts_in_window(conn, since_ts, until_ts)
    posts = [p for p in posts if p.get("post_type") != "retweet"]
    # Keep a broader, but still bounded, editorial input than the old digest
    # candidate filter. Ranking only governs truncation, never story grouping.
    posts = sorted(posts, key=_score, reverse=True)[:MAX_POSTS]
    input_ids = [str(p["id"]) for p in posts]
    ledger = _ledger_for_prompt(conn)
    review_id = herald_db.save_story_desk_review(conn, since_ts, until_ts, input_ids)
    conn.commit()
    if not posts:
        herald_db.finish_story_desk_review(conn, review_id, "{}", None, "skipped")
        conn.commit()
        return [], {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}, "none", "skipped"

    if dry_run:
        raw = {"candidates": [{
            "lead_post_id": input_ids[0], "anchor": "Dry-run timeline pick",
            "story_type": "one_shot", "state": "watching", "urgency": "normal",
            "readiness": 0.0, "rationale": "No model call made.",
            "sources": [{"post_id": input_ids[0], "role": "lead", "relevance": 1.0}],
        }]}
        usage, model, finish = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}, "dry-run", "stop"
        raw_text = json.dumps(raw)
    else:
        user = (
            "STORY LEDGER (local, incomplete; IDs may be used only for concrete continuations):\n"
            + json.dumps(ledger, ensure_ascii=False)
            + "\n\nPOSTS:\n" + json.dumps(_serialise(posts), ensure_ascii=False)
            + "\n\nReturn desk candidate JSON now."
        )
        raw_text, usage, model, finish = herald_synthesise._call(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            model=DESK_MODEL,
            max_tokens=herald_synthesise.STAGE1_MAX_TOKENS,
            max_reasoning_tokens=herald_synthesise.STAGE1_MAX_REASONING_TOKENS,
        )
        raw = _parse_response(raw_text)

    candidates = _normalise(
        raw, set(input_ids), {str(p["id"]): p.get("text") or "" for p in posts},
        {row["id"] for row in ledger},
    )
    for candidate in candidates:
        herald_db.upsert_story_candidate(conn, candidate, candidate["sources"], model=model, raw_output=raw_text)
    herald_db.finish_story_desk_review(
        conn, review_id, raw_text, model, "success" if candidates or dry_run else "empty"
    )
    conn.commit()
    return candidates, usage, model, finish


def replay_review(conn, review_id):
    """Parse and persist an already-paid desk response without another LLM call."""
    row = conn.execute("SELECT * FROM story_desk_reviews WHERE id=?", (int(review_id),)).fetchone()
    if not row:
        raise ValueError(f"story desk review {review_id} does not exist")
    try:
        input_ids = {str(pid) for pid in json.loads(row["input_post_ids"] or "[]")}
    except (TypeError, json.JSONDecodeError):
        input_ids = set()
    raw_text = row["raw_output"] or ""
    post_texts = {}
    if input_ids:
        placeholders = ",".join("?" for _ in input_ids)
        text_rows = conn.execute(
            f"SELECT external_id, text FROM posts WHERE source='x' AND external_id IN ({placeholders})",
            list(input_ids),
        ).fetchall()
        post_texts = {str(text_row["external_id"]): text_row["text"] or "" for text_row in text_rows}
    candidates = _normalise(
        _parse_response(raw_text), input_ids, post_texts,
        {row["id"] for row in _ledger_for_prompt(conn)},
    )
    for candidate in candidates:
        herald_db.upsert_story_candidate(
            conn, candidate, candidate["sources"], model=row["model"], raw_output=raw_text,
        )
    herald_db.finish_story_desk_review(
        conn, int(review_id), raw_text, row["model"], "success" if candidates else "empty",
    )
    conn.commit()
    return candidates, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}, row["model"], "replay"


def gather_evidence(conn, state="ready", candidate_ids=None, dry_run=False):
    """Earn bounded quote/link evidence for persistent candidate(s).

    The desk deliberately spends this only on candidates the editor has marked
    ready (or an operator explicitly selects).  It is not a background crawl.
    """
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = conn.execute(
            f"SELECT * FROM story_candidates WHERE id IN ({placeholders}) ORDER BY last_seen DESC",
            [int(cid) for cid in candidate_ids],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM story_candidates WHERE state=? ORDER BY readiness DESC, last_seen DESC",
            (state,),
        ).fetchall()
    results = []
    # One desk review is one editorial action. Its evidence allowance is
    # shared across all selected candidates so a large ready queue cannot
    # turn into a hidden crawl.
    budget = {"context": DESK_CONTEXT_READS, "links": DESK_LINK_FETCHES,
              "images": 0, "threads": DESK_THREAD_FETCHES}
    for row in rows:
        candidate = dict(row)
        posts = herald_db.get_story_candidate_posts(conn, candidate["id"])
        diagnostics = herald_evidence.enrich_story_posts(posts, budget, dry_run=dry_run)
        quote_ids = []
        thread_ids = []
        link_urls = []
        for post in posts:
            for item in post.get("_quote_context") or []:
                tweet = item.get("tweet") if isinstance(item, dict) else item
                if isinstance(tweet, dict) and tweet.get("id"):
                    quote_ids.append(str(tweet["id"]))
            thread_ids.extend(str(tweet.get("id")) for tweet in post.get("_thread_context") or [] if tweet.get("id"))
            link_urls.extend(entry.get("url") for entry in post.get("_link_context") or [] if entry.get("url"))
        herald_db.add_story_candidate_evidence(
            conn, candidate["id"], "targeted_context", candidate["anchor_key"], "success",
            {"quote_post_ids": sorted(set(quote_ids)), "thread_post_ids": sorted(set(thread_ids)), "diagnostics": diagnostics},
        )
        for url in sorted(set(link_urls)):
            herald_db.add_story_candidate_evidence(conn, candidate["id"], "linked_article", url, "success")
        results.append({"id": candidate["id"], "anchor": candidate["anchor"],
                        "source_count": len(posts), "evidence": diagnostics,
                        "quote_post_ids": sorted(set(quote_ids)), "link_urls": sorted(set(link_urls)),
                        "thread_post_ids": sorted(set(thread_ids)),
                        "budget_remaining": dict(budget)})
    conn.commit()
    return results


def packet(conn, candidate_id):
    """Return a writer-ready, inspectable evidence packet without new fetches."""
    row = conn.execute("SELECT * FROM story_candidates WHERE id=?", (int(candidate_id),)).fetchone()
    if not row:
        raise ValueError(f"story candidate {candidate_id} does not exist")
    candidate = dict(row)
    posts = herald_db.get_story_candidate_posts(conn, candidate_id)
    # A zero allowance attaches only context already earned and cached. It
    # cannot issue a Bird read; link attachment is likewise cache-only.
    herald_evidence.enrich_story_posts(posts, {"context": 0, "links": 0, "images": 0})
    herald_links.attach_link_context(posts)
    sources = []
    for post in posts:
        quote_chain = []
        for item in post.get("_quote_context") or []:
            tweet = item.get("tweet") if isinstance(item, dict) else item
            if not isinstance(tweet, dict):
                continue
            quote_chain.append({
                "id": str(tweet.get("id")),
                "author": (tweet.get("author") or {}).get("username", "unknown"),
                "text": tweet.get("text") or "",
                "depth": item.get("_depth") if isinstance(item, dict) else None,
            })
        links = [{"url": entry.get("url"), "excerpt": (entry.get("text") or "")[:1200]}
                 for entry in post.get("_link_context") or []]
        thread_context = [{"id": str(tweet.get("id")),
                           "author": (tweet.get("author") or {}).get("username", "unknown"),
                           "text": tweet.get("text") or ""}
                          for tweet in post.get("_thread_context") or []]
        sources.append({
            "id": str(post.get("id")), "role": post.get("desk_role", "source"),
            "relevance": post.get("desk_relevance", 1.0),
            "author": (post.get("author") or {}).get("username", "unknown"),
            "text": post.get("text") or "", "quote_chain": quote_chain,
            "thread_context": thread_context, "links": links,
        })
    evidence_rows = conn.execute(
        """SELECT evidence_kind, reference_id, status, data, observed_at
            FROM story_candidate_evidence WHERE candidate_id=? ORDER BY id""", (int(candidate_id),)
    ).fetchall()
    evidence = []
    for evidence_row in evidence_rows:
        item = dict(evidence_row)
        try:
            item["data"] = json.loads(item["data"]) if item.get("data") else None
        except (TypeError, json.JSONDecodeError):
            pass
        evidence.append(item)
    return {
        "candidate": candidate, "sources": sources, "evidence": evidence,
        "readiness": herald_db.assess_story_candidate(conn, candidate_id),
    }


def write_draft(conn, candidate_id, dry_run=False):
    """Write and persist an unpublished draft from an already-earned packet."""
    row = conn.execute("SELECT * FROM story_candidates WHERE id=?", (int(candidate_id),)).fetchone()
    if not row:
        raise ValueError(f"story candidate {candidate_id} does not exist")
    candidate = dict(row)
    posts = herald_db.get_story_candidate_posts(conn, candidate_id)
    # Cache-only attachment: a draft must not quietly earn more X context.
    herald_evidence.enrich_story_posts(posts, {"context": 0, "links": 0, "images": 0, "threads": 0})
    herald_links.attach_link_context(posts)
    roles = {str(post.get("id")): post.get("desk_role", "source") for post in posts}
    role_note = (
        "Use the supplied role labels as evidence structure: lead establishes the proposition; "
        "reaction shows timeline response; counterpoint carries substantive challenge; "
        "colour is supporting detail; background is earlier context. Do not present a "
        "background source as a newly breaking development."
    )
    draft, usage, model, finish = herald_synthesise.stage2_story(
        candidate["anchor"], posts, angle=candidate.get("rationale"), editor_notes=role_note,
        model=DESK_WRITER_MODEL, dry_run=dry_run, source_roles=roles,
    )
    raw_output = draft.pop("_raw_output", None)
    draft.pop("_quote_rejections", None)
    draft.pop("_citation_rejections", None)
    draft_id = herald_db.save_story_candidate_draft(conn, candidate_id, draft, raw_output=raw_output)
    conn.commit()
    return draft_id, draft, usage, model, finish


def replay_draft(conn, draft_id):
    """Repair an already-paid draft after a response-parser improvement."""
    row = conn.execute("SELECT * FROM story_candidate_drafts WHERE id=?", (int(draft_id),)).fetchone()
    if not row or not row["raw_output"]:
        raise ValueError(f"draft {draft_id} has no raw model output to replay")
    candidate = conn.execute("SELECT * FROM story_candidates WHERE id=?", (row["candidate_id"],)).fetchone()
    if not candidate:
        raise ValueError(f"draft {draft_id} has no story candidate")
    posts = herald_db.get_story_candidate_posts(conn, row["candidate_id"])
    herald_evidence.enrich_story_posts(posts, {"context": 0, "links": 0, "images": 0, "threads": 0})
    try:
        draft = herald_synthesise.parse_json_response(row["raw_output"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"draft {draft_id} is not recoverable JSON: {exc}") from exc
    if not isinstance(draft, dict) or not draft.get("headline") or not draft.get("story"):
        raise ValueError(f"draft {draft_id} lacks headline or story")
    _ok, bad_citations = herald_synthesise._validate_story_citations(draft["story"], posts)
    if bad_citations:
        raise ValueError(f"draft {draft_id} has invalid citations: {bad_citations}")
    valid_quotes, _rejected = herald_synthesise._validate_key_quotes(draft.get("key_quotes"), posts)
    candidate = dict(candidate)
    draft["key_quotes"] = valid_quotes
    draft["coverage_type"] = draft.get("coverage_type") or "one_shot"
    draft["section"] = "story"
    draft["theme_label"] = candidate["anchor"]
    draft["angle"] = candidate.get("rationale")
    draft["model_version"] = row["model"]
    draft["prompt_tokens"] = row["prompt_tokens"] or 0
    draft["completion_tokens"] = row["completion_tokens"] or 0
    draft["cost_usd"] = row["cost_usd"] or 0.0
    conn.execute(
        "UPDATE story_candidate_drafts SET headline=?, story=?, data=? WHERE id=?",
        (draft["headline"], draft["story"], json.dumps(draft, ensure_ascii=False), int(draft_id)),
    )
    conn.commit()
    return draft


def create_issue(conn, candidate_ids=None, limit=6, title=None):
    """Select private drafts into an on-demand, unpublished issue."""
    issue_id = herald_db.create_story_issue(
        conn, candidate_ids=candidate_ids, limit=limit, title=title,
    )
    conn.commit()
    return herald_db.get_story_issue(conn, issue_id)


def publish_issue(conn, issue_id):
    """Publish one reviewed issue without triggering collection or an LLM."""
    issue = herald_db.publish_story_issue(conn, issue_id)
    conn.commit()
    return issue


def render_issue_markdown(issue):
    """Operator-facing issue preview. This is intentionally not web publishing."""
    title = issue.get("title") or f"Herald issue #{issue['id']}"
    lines = [f"# {title}", "", "private editorial draft · not published", ""]
    if not issue.get("items"):
        lines.extend(["No candidate drafts were selected.", ""])
    for item in issue.get("items") or []:
        lines.extend([f"## {item['position']}. {item.get('headline') or item['anchor']}", ""])
        if item.get("continuity_parent_id") or item.get("continuity_note"):
            parent = item.get("continuity_parent_id")
            label = f"Continuation of candidate #{parent}" if parent else "Continuing story"
            lines.extend([f"_{label}: {item.get('continuity_note') or ''}_", ""])
        lines.extend([item.get("story") or "", ""])
    return "\n".join(lines).strip() + "\n"


def render_packet_markdown(data):
    """Compact human review view; source text stays verbatim and attributed."""
    candidate = data["candidate"]
    lines = [
        f"# {candidate['anchor']}", "",
        f"state: {candidate['state']} · {candidate['story_type']} · readiness {candidate['readiness']:.2f} · {candidate['urgency']}",
        "", candidate.get("rationale") or "", "", "## Sources", "",
    ]
    if candidate.get("continuity_parent_id") or candidate.get("continuity_note"):
        parent = candidate.get("continuity_parent_id")
        label = f"Continuity from candidate #{parent}" if parent else "Continuity"
        lines[3:3] = [
            "", f"{label}: "
            f"{candidate.get('continuity_note') or 'later development of an earlier story.'}",
        ]
    readiness = data.get("readiness") or {}
    if readiness:
        lines[5:5] = ["", f"Readiness check: {'passes' if readiness.get('ready') else 'waiting'} — {readiness.get('reason', '')}"]
    for source in data["sources"]:
        lines.extend([
            f"### {source['role']} · @{source['author']} · [post:{source['id']}]", "",
            source["text"], "",
        ])
        if source["quote_chain"]:
            lines.append("Quoted context:")
            for quote in source["quote_chain"]:
                lines.extend([f"> @{quote['author']} · [post:{quote['id']}]", f"> {quote['text']}", ">"])
            lines.append("")
        if source["thread_context"]:
            lines.append("Collected author thread:")
            for thread_post in source["thread_context"]:
                lines.extend([f"> @{thread_post['author']} · [post:{thread_post['id']}]", f"> {thread_post['text']}", ">"])
            lines.append("")
        for link in source["links"]:
            lines.extend([f"Linked reporting: {link['url']}", "", link["excerpt"], ""])
    return "\n".join(lines).strip() + "\n"
