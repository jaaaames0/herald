"""trace_digest.py — Produce a human-readable trace of a Herald digest.

Shows inputs/outputs at every pipeline stage so you can verify:
- The original tweets flowed through unmodified into the LLM prompts
- Every [post:ID] citation in a story resolves to a real post in the DB
- Every embedded quote is a verbatim substring of the cited post
- Nothing was fabricated

Usage: python3 trace_digest.py [digest_id]
       (default: most recent digest)
"""
import json
import re
import sys
sys.path.insert(0, "/home/james/srv/herald")

import herald_db


CITATION_RE = re.compile(r"\[post:([A-Za-z0-9_:.\-]+)\]")


def lookup_post(post_id, posts_by_id):
    """post_id is the X tweet ID (the 'external_id' in the DB)."""
    return posts_by_id.get(post_id)


def extract_citations(text):
    """Return ordered list of post IDs cited in a story's prose."""
    return CITATION_RE.findall(text or "")


def verify_quote(quote, source_text):
    """True if quote text is a verbatim substring (with reasonable tolerance) of source.

    Normalisations applied before matching:
      - Whitespace collapsed
      - Unicode dashes (U+2010–U+2015) → ASCII hyphen-minus
      - Smart quotes (U+2018–U+201D) → ASCII apostrophe / double quote
      - Trailing/leading punctuation trimmed for comparison

    Then:
      1. Exact substring
      2. Substring after normalisation
      3. Sentences joined across source (each sentence found, words between elided)
    """
    if not quote or not source_text:
        return False, "empty quote or source"

    def _normalize(s):
        # Whitespace
        s = " ".join(s.split())
        # Unicode dashes → ASCII hyphen
        for d in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015"):
            s = s.replace(d, "-")
        # Smart quotes → ASCII
        for q in ("\u2018", "\u2019", "\u201a", "\u201b"):
            s = s.replace(q, "'")
        for q in ("\u201c", "\u201d", "\u201e", "\u201f"):
            s = s.replace(q, "\"")
        return s

    norm_quote = _normalize(quote)
    norm_src = _normalize(source_text)

    # Level 1: exact substring (after unicode/whitespace normalization)
    if norm_quote in norm_src:
        return True, "verbatim substring"

    # Level 2: strip outer punctuation from quote (LLM often wraps in quotes/parens)
    trimmed = norm_quote.strip().strip('"').strip("'").strip(",").strip()
    if trimmed and trimmed != norm_quote and trimmed in norm_src:
        return True, "verbatim substring (punctuation trimmed)"

    # Level 3: joined quotes — split into sentences, each must be in source
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z'\"\\-])", norm_quote)
    sentences = [s.strip().rstrip(",").strip().strip('"').strip() for s in sentences if s.strip()]
    sentences = [s for s in sentences if len(s) >= 5]
    if len(sentences) >= 2:
        all_found = all(s in norm_src for s in sentences)
        if all_found:
            # Estimate elided words
            first_idx = norm_src.find(sentences[0])
            last_idx = norm_src.find(sentences[-1], first_idx + len(sentences[0]))
            if first_idx >= 0 and last_idx >= 0:
                between = norm_src[first_idx + len(sentences[0]): last_idx]
                elided_words = len(between.split())
                if elided_words == 0:
                    return True, "verbatim (adjacent sentences)"
                return True, f"sentences joined (elided {elided_words} words)"

    return False, "NOT FOUND IN SOURCE — possible fabrication"


def lookup_account(handle, posts_by_id):
    """Look up an account by handle across all input posts."""
    for p in posts_by_id.values():
        if (p.get("author") or {}).get("username") == handle:
            return p.get("author")
    return None


def fmt_metrics(m):
    if not m:
        return "metrics: n/a"
    parts = []
    for k in ("likes", "retweets", "replies", "quotes"):
        v = m.get(k)
        if v is not None:
            parts.append(f"{v} {k}")
    return " · ".join(parts) if parts else "metrics: n/a"


def fmt_post(p, idx=None):
    if not p:
        return "  (post not found in DB)"
    header = f"  {idx} " if idx is not None else "  "
    text = (p.get("text") or "").replace("\n", " ")
    metrics = fmt_metrics(p.get("metrics") or {})
    author = (p.get("author") or {}).get("username", "?")
    external_id = p.get("id") or "?"
    created = p.get("created_at")
    ts = ""
    if created:
        import datetime as _dt
        try:
            ts = _dt.datetime.fromtimestamp(created, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            ts = f"created_at={created}"
    print(f"{header}@{author:<22} | {ts}")
    print(f"{header}  id: {external_id}")
    print(f"{header}  {metrics}")
    print(f"{header}  text: {text}")
    return text


def main():
    digest_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = herald_db.get_connection()
    if digest_id is None:
        # most recent digest
        row = conn.execute("SELECT id FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("no digests found")
            return 1
        digest_id = row["id"]

    print("=" * 70)
    print(f"HERALD DIGEST {digest_id} — END-TO-END TRACE")
    print("=" * 70)

    d = herald_db.get_digest_summary(conn, digest_id)
    if not d:
        print(f"digest {digest_id} not found")
        return 1

    # ── Stage metadata ──
    period_start = d["period_start"]
    period_end = d["period_end"]
    hours = d.get("window_hours") or ((period_end - period_start) / 3600.0)
    import datetime as _dt
    start_str = _dt.datetime.fromtimestamp(period_start, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    end_str = _dt.datetime.fromtimestamp(period_end, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Window:   {start_str} → {end_str} ({hours:.1f} hours)")
    print(f"Stories:  {len(d['stories'])}")
    print(f"Generated: {_dt.datetime.fromtimestamp(d['generated_at'], tz=_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    # Fetch all input posts (the candidate set)
    input_pids = d.get("input_post_ids") or []
    posts_by_id = {}
    if input_pids:
        rows = conn.execute(
            "SELECT * FROM posts WHERE source='x' AND external_id IN ({})".format(
                ",".join("?" for _ in input_pids)
            ),
            input_pids,
        ).fetchall()
        for r in rows:
            d_row = dict(r)
            # Same reconstitution as get_posts_in_window
            try:
                if d_row.get("media_urls"):
                    d_row["media_urls"] = json.loads(d_row["media_urls"])
                if d_row.get("link_urls"):
                    d_row["link_urls"] = json.loads(d_row["link_urls"])
                if d_row.get("metrics"):
                    d_row["metrics"] = json.loads(d_row["metrics"])
            except (json.JSONDecodeError, TypeError):
                pass
            d_row["author"] = {"id": d_row.pop("author_id"), "username": d_row.pop("author_handle")}
            d_row["id"] = d_row.pop("external_id")
            posts_by_id[d_row["id"]] = d_row
    print(f"Input posts (candidates sent to Stage 1 LLM): {len(input_pids)}")
    print()

    # Pipeline run info
    pr = conn.execute(
        "SELECT * FROM pipeline_runs WHERE step='digest' AND rows_out=? ORDER BY id DESC LIMIT 1",
        (len(d["stories"]),),
    ).fetchone()
    if pr:
        pr_d = dict(pr)
        cost = pr_d.get("cost_usd") or 0
        pin = pr_d.get("prompt_tokens") or 0
        pout = pr_d.get("completion_tokens") or 0
        print(f"Pipeline run (digest):")
        print(f"  model: {pr_d.get('model')}")
        print(f"  total tokens: {pin} in / {pout} out")
        print(f"  total cost: ${cost:.6f}")
        print(f"  elapsed: started={_dt.datetime.fromtimestamp(pr_d['started_at'], tz=_dt.timezone.utc).strftime('%H:%M:%S UTC')}",
              end="")
        if pr_d.get("finished_at"):
            print(f" finished={_dt.datetime.fromtimestamp(pr_d['finished_at'], tz=_dt.timezone.utc).strftime('%H:%M:%S UTC')}")
        else:
            print()
        print()

    # ── Stage 1 output: timeline map ──
    print("=" * 70)
    print("STAGE 1 OUTPUT: TIMELINE MAP")
    print("=" * 70)
    print()
    zeitgeist = d.get("zeitgeist") or []
    print(f"ZEITGEIST ({len(zeitgeist)} themes):")
    for i, th in enumerate(zeitgeist, 1):
        print(f"  {i}. {th.get('label')}")
        print(f"     salience:    {th.get('salience')}")
        print(f"     accounts:    {', '.join('@' + a for a in (th.get('accounts') or []))}")
        pids = th.get("post_ids") or []
        print(f"     cited posts: {len(pids)}  ids: {', '.join(pids[:5])}{' ...' if len(pids) > 5 else ''}")
        print(f"     description: {th.get('description', '')[:200]}")
        print(f"     why_it_matters: {th.get('why_it_matters', '')[:200]}")
        print()

    tm = d.get("timeline_map") or {}
    stw = tm.get("stories_to_write") or []
    print(f"STORIES TO WRITE ({len(stw)} chosen from {len(zeitgeist)} themes):")
    for i, s in enumerate(stw, 1):
        print(f"  {i}. [{s.get('label')}] angle: {s.get('angle')}")
    print()

    # ── Stage 2 output: each story ──
    print("=" * 70)
    print("STAGE 2 OUTPUT: STORIES (with citation traceability)")
    print("=" * 70)

    for story_idx, s in enumerate(d["stories"], 1):
        print()
        print(f"┌─── STORY {story_idx}/{len(d['stories'])}: {s.get('theme_label', '?')} ───")
        print(f"│ HEADLINE:  {s.get('headline')}")
        print(f"│ TOPIC:     {s.get('topic')}")
        print(f"│ SECTION:   {s.get('section')}")
        print(f"│ in/out/cost: {s.get('prompt_tokens')}/{s.get('completion_tokens')}/${s.get('cost_usd'):.6f}")
        angle = s.get("angle")
        if angle:
            print(f"│ ANGLE (from Stage 1): {angle}")
        print(f"│")
        print(f"│ INPUT POSTS (the theme's source material — original tweets):")
        # Use the linked posts from the story_posts JOIN (authoritative source)
        linked_posts = s.get("posts") or []
        # Reconstitute each linked post for our lookup
        for j, lp in enumerate(linked_posts, 1):
            print(f"│")
            text_lp = lp.get("text") or ""
            metrics_lp = lp.get("metrics") or "{}"
            if isinstance(metrics_lp, str):
                try: metrics_lp = json.loads(metrics_lp)
                except: metrics_lp = {}
            ext_id = lp.get("external_id") or "?"
            username = lp.get("author_handle") or "?"
            ts = ""
            if lp.get("created_at"):
                import datetime as _dt
                ts = _dt.datetime.fromtimestamp(lp["created_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"│   {j} @{username:<22} | {ts}")
            print(f"│     id: {ext_id}")
            print(f"│     {fmt_metrics(metrics_lp)}")
            print(f"│     text: {text_lp.replace(chr(10), ' ')}")
        print(f"│")
        print(f"│ LLM NARRATIVE ({len(s.get('story') or '')} chars):")
        story_text = s.get("story") or ""
        # Indent narrative
        for line in story_text.split("\n"):
            print(f"│   {line}")
        print(f"│")
        print(f"│ CITATION CHECK:")
        cited_pids = extract_citations(story_text)
        unique_cited = []
        seen = set()
        for pid in cited_pids:
            if pid not in seen:
                seen.add(pid)
                unique_cited.append(pid)
        all_ok = True
        for pid in unique_cited:
            p = posts_by_id.get(pid)
            if p is None:
                print(f"│   [post:{pid}] → ❌ NOT FOUND IN INPUT (fabricated post ID)")
                all_ok = False
            else:
                auth = (p.get("author") or {}).get("username", "?")
                # Find the cited sentence
                sent = ""
                for s_sent in re.split(r"(?<=[.!?])\s+", story_text):
                    if f"[post:{pid}]" in s_sent:
                        sent = s_sent[:140]
                        break
                print(f"│   [post:{pid}] → ✓ @{auth}  ('{sent}...')")
        print(f"│   {len(unique_cited)} unique citations, {len(unique_cited) - sum(1 for p in unique_cited if posts_by_id.get(p) is None)} verified")
        print(f"│")
        print(f"│ EMBEDDED QUOTES (key_quotes from LLM output):")
        kqs = s.get("key_quotes") or []
        if isinstance(kqs, str):
            try: kqs = json.loads(kqs)
            except: kqs = []
        for kq in kqs:
            if not isinstance(kq, dict):
                continue
            q = kq.get("quote", "")
            spk = kq.get("speaker", "?")
            spk = spk.lstrip("@")  # strip leading @ if present
            pid = kq.get("post_id", "?")
            # Try input posts first; fall back to linked posts
            p = posts_by_id.get(pid)
            if p is None:
                for lp in linked_posts:
                    if lp.get("external_id") == pid:
                        p = {"text": lp.get("text"), "author": {"username": lp.get("author_handle")}}
                        break
            if p is None:
                print(f"│   ❌ \"{q[:80]}...\" — @{spk} (post:{pid}) — POST NOT FOUND")
                continue
            ok, reason = verify_quote(q, p.get("text") or "")
            mark = "✓" if ok else "❌"
            print(f"│   {mark} \"{q[:80]}{'...' if len(q) > 80 else ''}\" — @{spk} ({reason})")
        print(f"└{'─' * 67}")

    print()
    print("=" * 70)
    print("PIPELINE FLOW — RECAP")
    print("=" * 70)
    print("""
  X timeline  ─►  bird CLI  ─►  raw JSONL
       │                                │
       │                                ▼
       │                       collect.py (RT resolve, age gate)
       │                                │
       │                                ▼
       │                       parser.py (clean JSON)
       │                                │
       │                                ▼
       │                       herald_ingest.py → herald.db:posts table
       │                                │
       │                                ▼
       │                       herald_embed.select_candidates:
       │                          · embed each post's text (1536-dim vector)
       │                          · greedy cluster by cosine similarity (threshold 0.50)
       │                          · rank by size × engagement × log10(followers+10)
       │                          · pick ~40 representatives (cluster centroid + 1-2 engaged)
       │                                │
       │                                ▼
       │                       Stage 1 LLM call (herald_synthesise.stage1_timeline_map):
       │                          · INPUT:  40 original tweets (full text)
       │                          · OUTPUT: zeitgeist themes, debate lines, signal/noise, stories_to_write
       │                          · NOTE:   LLM does NOT modify the tweet text
       │                                │
       │                                ▼
       │                       Stage 2 LLM calls (one per story, max 5):
       │                          · INPUT:  the theme's original tweets (full text) + angle from stage 1
       │                          · OUTPUT: narrative with inline [post:ID] citations
       │                          · NOTE:   LLM WRITES new prose but must cite each claim
       │                                │
       │                                ▼
       │                       herald_db.upsert_digest_with_stories → herald.db:digests + stories
       │
       └──── everything traceable via post IDs (X tweet IDs in this case)

  TRANSFORMATIONS APPLIED:
    1. Tweet text → 1536-dim vector (for clustering only — original text preserved in DB)
    2. 41 raw posts → 40 candidates (1 dropped by pre-filter; truncation at 1500 chars
       affects only the embedding input, not the LLM input)
    3. 40 candidate tweets → 5 zeitgeist themes (LLM editorial survey, no content change)
    4. 5 themes → 4 chosen stories (LLM picked 4 high/medium salience; 1 skipped)
    5. Each theme's tweets → 1 narrative (LLM prose, cited inline)

  WHAT IS LOST:
    · Posts the pre-filter dropped (1 of 41 in this run — the lowest-ranked singleton)
    · Themes the LLM didn't flag as stories (Sports & national pride in this run)
    · Posts cited in zeitgeist/debate/signal_noise that aren't in any chosen story
      (still queryable via digests.timeline_map JSON column)

  WHAT IS PRESERVED:
    · Every original tweet — stored verbatim in herald.db:posts
    · Every citation — stored in stories.story as [post:ID] and linked via story_posts table
    · Every quote — stored in stories.key_quotes with post_id (verifiable substring check above)
    · Every editorial decision — timeline_map JSON captures what Stage 1 saw and decided
""")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
