"""Herald's DB layer: connection helper, schema init, upserts, pipeline-run logging."""
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path


DB_PATH = Path(os.environ.get("HERALD_DB_PATH", "/var/lib/herald/herald.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------- cache-table helpers (PR 2 refactor MR-1) ----------

def _protected_set_clause(table, col):
    """Return the ON CONFLICT assignment that preserves `col` unless the
    incoming row is a success. All four cache tables (image, link,
    conversation, quote) follow this pattern: a failed retry must not
    clobber a previously-stored good extraction. Keeps the SQL literal
    — no dynamic column names — so the query is readable and the columns
    are still obvious in each function."""
    return (f"{col} = CASE WHEN excluded.fetch_status = 'success' "
            f"THEN excluded.{col} ELSE {table}.{col} END")


def get_connection():
    """Open a SQLite connection to Herald's DB. Creates the parent dir if needed.

    Uses WAL journal mode so concurrent connections (collect + digest
    running on overlapping schedules, or test code that opens multiple
    connections) don't block on the rollback journal. WAL is safe for
    Herald's workload: many small writes, no long-running transactions.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL persists across connections; setting it on every connect is a
    # cheap no-op after the first call.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn):
    """Apply schema.sql (idempotent).

    CREATE TABLE/INDEX statements use IF NOT EXISTS so they're safe to re-run.
    ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite; we guard each with a
    pragma_table_info check before issuing the ALTER, so re-runs are no-ops.

    We strip SQL line comments (-- ...) before splitting on `;`, since the
    schema has semicolons inside -- comments (e.g. column descriptions).
    """
    import re as _re

    schema_sql = SCHEMA_PATH.read_text()
    # Strip line comments. Keep newlines so line numbers in errors stay sane.
    schema_sql = _re.sub(r"--[^\n]*", "", schema_sql)
    # Split on semicolons; skip blanks.
    stmts = [s.strip() for s in schema_sql.split(";") if s.strip()]
    for stmt in stmts:
        if stmt.lstrip().upper().startswith("ALTER TABLE"):
            # Matches: ALTER TABLE <name> ADD COLUMN <col> <type>
            m = _re.match(
                r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
                stmt, _re.IGNORECASE,
            )
            if m:
                table, col = m.group(1), m.group(2)
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col in cols:
                    continue  # already added on a prior run
        conn.execute(stmt)
    conn.commit()


def parse_twitter_ts(s):
    """Parse Twitter's `created_at` format to Unix epoch.
    Format: 'Sat Jun 27 06:08:59 +0000 2026'.
    Returns None on parse failure (caller decides how to handle)."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return int(dt.timestamp())
    except ValueError:
        return None


def upsert_post(conn, post):
    """Insert or update a post. ON CONFLICT (source, external_id) updates the
    mutable fields (author, text, media, etc.) and bumps ingested_at.

    Also persists conversation_id / quoted_tweet_id / in_reply_to_id from the
    parser's `conversation` block so herald_context can look up cached thread
    context by conversation_id during synthesis.
    """
    now = int(time.time())
    media  = json.dumps(post["media"])  if post.get("media")  is not None else None
    links  = json.dumps(post["links"])  if post.get("links")  is not None else None
    metrics = json.dumps(post["metrics"]) if post.get("metrics") is not None else None
    amplified_by = json.dumps(post["amplified_by"]) if post.get("amplified_by") else None

    # Extract conversation/quote fields from the parser's nested block. The
    # parser emits `conversation = {id, is_reply, parent_id, parent_tweet}`
    # and `quoted_tweet = {id, ...}`; flatten them so herald_context can look
    # up cached threads by conversation_id alone.
    conv = post.get("conversation") or {}
    conversation_id = conv.get("id") or None
    in_reply_to_id = conv.get("parent_id") or None
    quoted = post.get("quoted_tweet") or {}
    quoted_tweet_id = quoted.get("id") or None

    cur = conn.execute("""
        INSERT INTO posts (
          external_id, source, author_id, author_handle,
          created_at, ingested_at, text, media_urls, link_urls,
          metrics, post_type, amplified_by,
          conversation_id, quoted_tweet_id, in_reply_to_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, external_id) DO UPDATE SET
          author_id       = excluded.author_id,
          author_handle   = excluded.author_handle,
          text            = excluded.text,
          media_urls      = excluded.media_urls,
          link_urls       = excluded.link_urls,
          metrics         = excluded.metrics,
          post_type       = excluded.post_type,
          amplified_by    = excluded.amplified_by,
          conversation_id = excluded.conversation_id,
          quoted_tweet_id = excluded.quoted_tweet_id,
          in_reply_to_id  = excluded.in_reply_to_id,
          ingested_at     = excluded.ingested_at
    """, (
        post["id"],
        "x",
        post["author"]["id"],
        post["author"]["username"],
        parse_twitter_ts(post.get("created_at")),
        now,
        post.get("text"),
        media,
        links,
        metrics,
        post.get("post_type"),
        amplified_by,
        conversation_id,
        quoted_tweet_id,
        in_reply_to_id,
    ))

    # Seed quote_context for inline quotes so digest-time lookup finds the
    # text without re-fetching. The post row above still stores only the
    # quoted_tweet_id (row-size hygiene); the full quoted_tweet blob lives
    # in quote_context. Deeper levels (the quoted tweet's own quotedTweet)
    # are left as stubs here and chased by herald_context.enrich_batch.
    if quoted_tweet_id and quoted.get("text"):
        _seed_quote_context_inline(conn, quoted_tweet_id, quoted, now)

    return cur.lastrowid


def _seed_quote_context_inline(conn, quoted_id, quoted_blob, now_ts):
    """Persist an inline (parser-supplied) quoted tweet to quote_context.

    fetch_status='success' with fetch_error='inline-from-bird' marks the
    provenance; downstream code treats this identically to a fetched row.
    ON CONFLICT DO UPDATE so re-ingest always has the freshest inline data.
    """
    payload = json.dumps(quoted_blob, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO quote_context (
            quoted_tweet_id, fetched_at, fetch_status, fetch_error, data
        ) VALUES (?, ?, 'success', 'inline-from-bird', ?)
        ON CONFLICT(quoted_tweet_id) DO UPDATE SET
            fetched_at   = excluded.fetched_at,
            fetch_status = 'success',
            fetch_error  = 'inline-from-bird',
            data         = excluded.data
        """,
        (quoted_id, now_ts, payload),
    )


def upsert_account(conn, account, source="x"):
    """Insert or update account metadata. first_seen is set on insert only;
    last_seen is updated every ingest."""
    now = int(time.time())
    cur = conn.execute("""
        INSERT INTO accounts (
          id, source, handle, display_name, follower_count, verified,
          first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, id) DO UPDATE SET
          handle          = excluded.handle,
          display_name    = excluded.display_name,
          follower_count  = excluded.follower_count,
          last_seen       = excluded.last_seen
    """, (
        account["id"],
        source,
        account.get("username", ""),
        account.get("name", ""),
        account.get("followers_count", 0),
        False,  # verified not yet parsed from bird
        now,
        now,
    ))
    return cur.lastrowid


def start_pipeline_run(conn, step, source=None, rows_in=None):
    """Record the start of a pipeline step. Returns the run id."""
    now = int(time.time())
    cur = conn.execute("""
        INSERT INTO pipeline_runs (started_at, step, source, status, rows_in)
        VALUES (?, ?, ?, 'running', ?)
    """, (now, step, source, rows_in))
    return cur.lastrowid


def finish_pipeline_run(conn, run_id, status, rows_out=None, error=None,
                       prompt_tokens=None, completion_tokens=None,
                       cost_usd=None, model=None):
    """Mark a pipeline run complete with its final status, row counts,
    and (optional) aggregate token usage + cost for LLM-driven steps."""
    now = int(time.time())
    conn.execute("""
        UPDATE pipeline_runs
        SET finished_at = ?, status = ?, rows_out = ?, error = ?,
            prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, model = ?
        WHERE id = ?
    """, (now, status, rows_out, error,
          prompt_tokens, completion_tokens, cost_usd, model, run_id))


def get_posts_in_window(conn, since_ts, until_ts, source="x"):
    """Return posts where source matches and created_at is in [since_ts, until_ts).
    Each row is a dict matching the columns of `posts` (JSON fields deserialised,
    conversation/quote fields re-assembled into a `conversation` block to match the
    parser's shape)."""
    cur = conn.execute("""
        SELECT id, external_id, source, author_id, author_handle,
               created_at, ingested_at, text, media_urls, link_urls,
               metrics, post_type, amplified_by,
               conversation_id, quoted_tweet_id, in_reply_to_id
        FROM posts
        WHERE source = ? AND created_at >= ? AND created_at < ?
        ORDER BY created_at DESC
    """, (source, since_ts, until_ts))
    rows = cur.fetchall()
    posts = []
    for r in rows:
        d = dict(r)
        # Deserialise JSON fields; null in DB → None
        for f in ("media_urls", "link_urls", "metrics"):
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except (json.JSONDecodeError, TypeError):
                    d[f] = None
            else:
                d[f] = None
        # Reconstitute the author block the parser produces
        d["author"] = {
            "id": d.pop("author_id"),
            "username": d.pop("author_handle"),
        }
        if d.get("amplified_by"):
            try:
                d["amplified_by"] = json.loads(d["amplified_by"])
            except (json.JSONDecodeError, TypeError):
                d["amplified_by"] = None
        d["id"] = d.pop("external_id")  # use X tweet ID as the canonical id
        # Pop conversation/quote columns back into the parser's nested shape.
        # herald_context uses these to look up cached threads / quote parents.
        conversation_id = d.pop("conversation_id")
        in_reply_to_id = d.pop("in_reply_to_id")
        quoted_tweet_id = d.pop("quoted_tweet_id")
        d["conversation"] = {
            "id":           conversation_id,
            "parent_id":    in_reply_to_id,
            "is_reply":     bool(in_reply_to_id),
            "parent_tweet": None,
        }
        # When we have a quoted_tweet_id, surface a minimal block. parser.py
        # emits the full quoted_tweet with text inline when bird's --quote-depth
        # inlines it; that text is dropped here for row-size hygiene and lives
        # in quote_context (seeded by upsert_post for inline data, fetched by
        # herald_context.enrich_batch for stubs). attach_context_to_candidates
        # walks quote_context to assemble the chain at digest time.
        if quoted_tweet_id:
            d["quoted_tweet"] = {"id": quoted_tweet_id, "text": None}
        else:
            d["quoted_tweet"] = None
        posts.append(d)
    return posts


def record_timeline_capture(conn, post_ids, captured_at=None):
    """Persist one ordered home-timeline snapshot after posts have been ingested."""
    captured_at = int(captured_at or time.time())
    cur = conn.execute(
        "INSERT INTO timeline_captures (captured_at, post_count) VALUES (?, ?)",
        (captured_at, len(post_ids)),
    )
    capture_id = cur.lastrowid
    for position, external_id in enumerate(post_ids):
        row = conn.execute(
            "SELECT id, metrics FROM posts WHERE source='x' AND external_id=?", (str(external_id),)
        ).fetchone()
        if row:
            conn.execute(
                "INSERT OR IGNORE INTO capture_posts (capture_id, post_id, position) VALUES (?, ?, ?)",
                (capture_id, row["id"], position),
            )
            conn.execute(
                """INSERT OR REPLACE INTO post_observations
                   (capture_id, post_id, observed_at, position, metrics)
                   VALUES (?, ?, ?, ?, ?)""",
                (capture_id, row["id"], captured_at, position, row["metrics"]),
            )
    return capture_id


# ---------- Persistent story desk ----------

def save_story_desk_review(conn, period_start, period_end, input_post_ids,
                           raw_output=None, model=None, status="running"):
    """Record a review of active timeline material for reproducible tuning."""
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO story_desk_reviews
           (started_at, period_start, period_end, input_post_ids, raw_output, model, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now, int(period_start), int(period_end), json.dumps(input_post_ids), raw_output, model, status),
    )
    return cur.lastrowid


def finish_story_desk_review(conn, review_id, raw_output, model, status="success"):
    conn.execute(
        """UPDATE story_desk_reviews
           SET finished_at=?, raw_output=?, model=?, status=? WHERE id=?""",
        (int(time.time()), raw_output, model, status, review_id),
    )


def upsert_story_candidate(conn, candidate, post_roles, model=None, raw_output=None):
    """Upsert one accumulating desk candidate and its source-role membership.

    ``anchor_key`` is deterministic (currently the lead post ID), so repeated
    reviews enrich the same developing story instead of creating a new row.
    Published candidates retain their state until an explicit update workflow
    is added; a planner cannot silently reopen or overwrite a published item.
    """
    now = int(time.time())
    anchor_key = str(candidate["anchor_key"])
    state = candidate.get("state") if candidate.get("state") in {"watching", "ready"} else "watching"
    continuation_id = candidate.get("continuity_candidate_id")
    continuity_target = None
    if continuation_id:
        continuity_target = conn.execute(
            "SELECT id, state FROM story_candidates WHERE id=?", (int(continuation_id),)
        ).fetchone()
    # A stricter replay can identify a better lead for the same editorial
    # anchor. Re-key that unpublished candidate instead of leaving a stale
    # duplicate behind just because its first model pass chose a bad lead.
    existing = conn.execute(
        """SELECT id, anchor_key FROM story_candidates
           WHERE anchor=? AND state != 'published' ORDER BY id DESC LIMIT 1""",
        (candidate["anchor"],),
    ).fetchone()
    if existing and not continuity_target and existing["anchor_key"] != anchor_key:
        conn.execute("UPDATE story_candidates SET anchor_key=? WHERE id=?", (anchor_key, existing["id"]))
    # An unpublished continuation is one living story: keep its original
    # identity and accumulate the newly selected sources. A published story is
    # immutable; its later development becomes a linked, separate candidate.
    merge_target = continuity_target if continuity_target and continuity_target["state"] != "published" else None
    if merge_target:
        candidate_id = merge_target["id"]
        conn.execute(
            """UPDATE story_candidates SET anchor=?, story_type=?, state=?, rationale=?,
               readiness=?, urgency=?, last_seen=?, planner_model=?, planner_raw=?,
               continuity_note=? WHERE id=?""",
            (candidate["anchor"], candidate.get("story_type", "timeline_argument"), state,
             candidate.get("rationale"), float(candidate.get("readiness") or 0),
             candidate.get("urgency", "normal"), now, model, raw_output,
             candidate.get("continuity_note") or None, candidate_id),
        )
    else:
        continuity_parent_id = (continuity_target["id"]
                                if continuity_target and continuity_target["state"] == "published" else None)
        conn.execute(
        """INSERT INTO story_candidates
           (anchor_key, anchor, story_type, state, rationale, readiness, urgency,
            first_seen, last_seen, continuity_parent_id, continuity_note, planner_model, planner_raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(anchor_key) DO UPDATE SET
             anchor=excluded.anchor,
             story_type=excluded.story_type,
             state=CASE WHEN story_candidates.state='published' THEN 'published' ELSE excluded.state END,
             rationale=excluded.rationale,
             readiness=excluded.readiness,
             urgency=excluded.urgency,
             last_seen=excluded.last_seen,
             continuity_parent_id=COALESCE(story_candidates.continuity_parent_id, excluded.continuity_parent_id),
             continuity_note=COALESCE(excluded.continuity_note, story_candidates.continuity_note),
             planner_model=excluded.planner_model,
             planner_raw=excluded.planner_raw""",
        (anchor_key, candidate["anchor"], candidate.get("story_type", "timeline_argument"), state,
         candidate.get("rationale"), float(candidate.get("readiness") or 0),
         candidate.get("urgency", "normal"), now, now, continuity_parent_id,
         candidate.get("continuity_note") or None, model, raw_output),
        )
        row = conn.execute("SELECT id FROM story_candidates WHERE anchor_key=?", (anchor_key,)).fetchone()
        candidate_id = row["id"]
    # Lead roles are foundational claims, not historical colour. If a replay
    # rejects a former lead, remove it rather than letting it silently remain
    # in the writer packet. Other roles accumulate across desk reviews.
    retained_lead_ids = [str(post_id) for post_id, role, _ in post_roles if role == "lead"]
    if retained_lead_ids and merge_target:
        # Earlier leads are the historical frame for a continuing story, not
        # stale contamination. Preserve them explicitly for the writer.
        placeholders = ",".join("?" for _ in retained_lead_ids)
        conn.execute(
            f"""UPDATE story_candidate_posts SET role='background'
                WHERE candidate_id=? AND role='lead' AND post_id IN (
                  SELECT id FROM posts WHERE source='x' AND external_id NOT IN ({placeholders})
                )""",
            [candidate_id, *retained_lead_ids],
        )
    elif retained_lead_ids:
        placeholders = ",".join("?" for _ in retained_lead_ids)
        conn.execute(
            f"""DELETE FROM story_candidate_posts
                WHERE candidate_id=? AND role='lead' AND post_id IN (
                  SELECT id FROM posts WHERE source='x' AND external_id NOT IN ({placeholders})
                )""",
            [candidate_id, *retained_lead_ids],
        )
    for post_id, role, relevance in post_roles:
        post = conn.execute(
            "SELECT id FROM posts WHERE source='x' AND external_id=?", (str(post_id),)
        ).fetchone()
        if not post:
            continue
        conn.execute(
            """INSERT INTO story_candidate_posts
               (candidate_id, post_id, role, relevance, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(candidate_id, post_id) DO UPDATE SET
                 role=excluded.role, relevance=excluded.relevance, last_seen=excluded.last_seen""",
            (candidate_id, post["id"], role, float(relevance), now, now),
        )
    readiness = assess_story_candidate(conn, candidate_id)
    # The planner may nominate a candidate as ready, but it cannot bypass the
    # minimum evidence shape. A later desk pass can promote it once the missing
    # source has actually been captured.
    if state == "ready" and not readiness["ready"]:
        conn.execute(
            "UPDATE story_candidates SET state='watching', readiness=MIN(readiness, 0.65) WHERE id=? AND state != 'published'",
            (candidate_id,),
        )
    return candidate_id


def assess_story_candidate(conn, candidate_id):
    """Return the deterministic minimum-evidence verdict for one candidate.

    This is deliberately a floor, not an attempt to score newsworthiness. The
    desk model still decides what is worth following; the floor merely prevents
    a single unsupported post from being presented as a reported story.
    """
    candidate = conn.execute(
        "SELECT story_type FROM story_candidates WHERE id=?", (int(candidate_id),)
    ).fetchone()
    if not candidate:
        raise ValueError(f"story candidate {candidate_id} does not exist")
    rows = conn.execute(
        "SELECT role FROM story_candidate_posts WHERE candidate_id=?", (int(candidate_id),)
    ).fetchall()
    roles = [row["role"] for row in rows]
    linked = conn.execute(
        """SELECT count(*) AS n FROM story_candidate_evidence
           WHERE candidate_id=? AND evidence_kind='linked_article' AND status='success'""",
        (int(candidate_id),),
    ).fetchone()["n"]
    if "lead" not in roles:
        return {"ready": False, "reason": "needs a current lead post", "source_count": len(roles)}
    story_type = candidate["story_type"]
    if story_type == "one_shot":
        return {"ready": True, "reason": "self-contained one-shot with a lead", "source_count": len(roles)}
    support = {"reaction", "counterpoint", "colour", "background", "source"}
    has_support = any(role in support for role in roles)
    if story_type == "reported_event":
        if has_support or linked:
            return {"ready": True, "reason": "lead plus corroboration or linked reporting", "source_count": len(roles)}
        return {"ready": False, "reason": "reported event needs corroboration or linked reporting", "source_count": len(roles)}
    if len(roles) >= 2 and has_support:
        return {"ready": True, "reason": "lead plus a distinct timeline response or background", "source_count": len(roles)}
    return {"ready": False, "reason": "needs a lead and a distinct response, counterpoint, or background", "source_count": len(roles)}


def list_story_candidates(conn, state=None, limit=50):
    params = []
    where = ""
    if state:
        where = "WHERE sc.state=?"
        params.append(state)
    params.append(int(limit))
    rows = conn.execute(
        f"""SELECT sc.*, count(scp.post_id) AS source_count
            FROM story_candidates sc
            LEFT JOIN story_candidate_posts scp ON scp.candidate_id=sc.id
            {where}
            GROUP BY sc.id
            ORDER BY sc.last_seen DESC LIMIT ?""", params,
    ).fetchall()
    return [dict(row) for row in rows]


def get_story_candidate_posts(conn, candidate_id):
    """Return a desk candidate's sources in synthesis-compatible post shape."""
    rows = conn.execute(
        """SELECT p.*, scp.role AS desk_role, scp.relevance AS desk_relevance
            FROM story_candidate_posts scp JOIN posts p ON p.id=scp.post_id
            WHERE scp.candidate_id=? ORDER BY scp.relevance DESC, scp.first_seen ASC""",
        (int(candidate_id),),
    ).fetchall()
    posts = []
    for row in rows:
        d = dict(row)
        for field in ("media_urls", "link_urls", "metrics"):
            try:
                d[field] = json.loads(d[field]) if d.get(field) else []
            except (TypeError, json.JSONDecodeError):
                d[field] = []
        if d.get("amplified_by"):
            try:
                d["amplified_by"] = json.loads(d["amplified_by"])
            except (TypeError, json.JSONDecodeError):
                d["amplified_by"] = None
        d["id"] = str(d.pop("external_id"))
        d["links"] = d["link_urls"]
        d["media"] = d["media_urls"]
        d["author"] = {"id": d.pop("author_id"), "username": d.pop("author_handle")}
        d["conversation"] = {
            "id": d.get("conversation_id"), "parent_id": d.get("in_reply_to_id"),
            "is_reply": bool(d.get("in_reply_to_id")), "parent_tweet": None,
        }
        d["quoted_tweet"] = ({"id": d["quoted_tweet_id"], "text": None}
                             if d.get("quoted_tweet_id") else None)
        posts.append(d)
    return posts


def add_story_candidate_evidence(conn, candidate_id, evidence_kind, reference_id,
                                 status, data=None):
    """Append an auditable evidence observation to a persistent desk item."""
    conn.execute(
        """INSERT INTO story_candidate_evidence
           (candidate_id, evidence_kind, reference_id, status, data, observed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (int(candidate_id), evidence_kind, str(reference_id) if reference_id else None,
         status, json.dumps(data, ensure_ascii=False) if data is not None else None, int(time.time())),
    )


def save_story_candidate_draft(conn, candidate_id, draft, raw_output=None):
    """Persist an unpublished writer experiment for later editorial review."""
    cur = conn.execute(
        """INSERT INTO story_candidate_drafts
           (candidate_id, headline, story, data, raw_output, model, prompt_tokens,
            completion_tokens, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(candidate_id), draft.get("headline"), draft.get("story"),
         json.dumps(draft, ensure_ascii=False), raw_output, draft.get("model_version"),
         int(draft.get("prompt_tokens") or 0), int(draft.get("completion_tokens") or 0),
         float(draft.get("cost_usd") or 0), int(time.time())),
    )
    return cur.lastrowid


def candidate_drafts_for_issue(conn, candidate_ids=None, limit=6):
    """Return the newest private draft for each evidence-qualified candidate."""
    params = []
    where = "WHERE sc.state='ready'"
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        where += f" AND sc.id IN ({placeholders})"
        params.extend(int(candidate_id) for candidate_id in candidate_ids)
    rows = conn.execute(
        f"""SELECT sc.*, d.id AS draft_id, d.headline, d.story, d.data,
                   d.model AS draft_model, d.created_at AS draft_created_at,
                   count(scp.post_id) AS source_count
            FROM story_candidates sc
            JOIN story_candidate_drafts d ON d.id=(
              SELECT d2.id FROM story_candidate_drafts d2
              WHERE d2.candidate_id=sc.id ORDER BY d2.created_at DESC, d2.id DESC LIMIT 1
            )
            LEFT JOIN story_candidate_posts scp ON scp.candidate_id=sc.id
            {where}
            GROUP BY sc.id
            ORDER BY sc.urgency='breaking' DESC, sc.readiness DESC, sc.last_seen DESC
            LIMIT ?""",
        [*params, int(limit)],
    ).fetchall()
    selected = []
    for row in rows:
        item = dict(row)
        if not assess_story_candidate(conn, item["id"])["ready"]:
            continue
        selected.append(item)
    return selected


def create_story_issue(conn, candidate_ids=None, limit=6, title=None):
    """Create a private issue from the latest drafts; never publish it."""
    items = candidate_drafts_for_issue(conn, candidate_ids=candidate_ids, limit=limit)
    if candidate_ids:
        selected_ids = {item["id"] for item in items}
        missing = sorted(set(int(candidate_id) for candidate_id in candidate_ids) - selected_ids)
        if missing:
            raise ValueError(f"candidate(s) not ready with a draft: {', '.join(map(str, missing))}")
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO story_issues (title, state, created_at, updated_at) VALUES (?, 'draft', ?, ?)",
        (title or None, now, now),
    )
    issue_id = cur.lastrowid
    for position, item in enumerate(items, 1):
        conn.execute(
            """INSERT INTO story_issue_items (issue_id, candidate_id, draft_id, position)
               VALUES (?, ?, ?, ?)""",
            (issue_id, item["id"], item["draft_id"], position),
        )
    return issue_id


def get_story_issue(conn, issue_id):
    row = conn.execute("SELECT * FROM story_issues WHERE id=?", (int(issue_id),)).fetchone()
    if not row:
        raise ValueError(f"story issue {issue_id} does not exist")
    issue = dict(row)
    items = conn.execute(
        """SELECT sii.position, sc.id AS candidate_id, sc.anchor, sc.story_type, sc.urgency,
                   sc.continuity_parent_id, sc.continuity_note,
                   d.id AS draft_id, d.headline, d.story, d.data, d.model
            FROM story_issue_items sii
            JOIN story_candidates sc ON sc.id=sii.candidate_id
            JOIN story_candidate_drafts d ON d.id=sii.draft_id
            WHERE sii.issue_id=? ORDER BY sii.position""",
        (int(issue_id),),
    ).fetchall()
    issue["items"] = [dict(item) for item in items]
    return issue


def publish_story_issue(conn, issue_id):
    """Make an explicitly assembled issue public; no prose is regenerated."""
    issue = get_story_issue(conn, issue_id)
    if not issue["items"]:
        raise ValueError(f"story issue {issue_id} has no items")
    if issue["state"] == "published":
        return issue
    if issue["state"] != "draft":
        raise ValueError(f"story issue {issue_id} is not publishable from state {issue['state']!r}")
    now = int(time.time())
    conn.execute(
        "UPDATE story_issues SET state='published', published_at=?, updated_at=? WHERE id=?",
        (now, now, int(issue_id)),
    )
    return get_story_issue(conn, issue_id)


def list_story_issues(conn, limit=20):
    """List private on-demand issues with their selected-item count."""
    rows = conn.execute(
        """SELECT si.*, count(sii.candidate_id) AS item_count
           FROM story_issues si
           LEFT JOIN story_issue_items sii ON sii.issue_id=si.id
           GROUP BY si.id ORDER BY si.updated_at DESC, si.id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_public_story_issues(conn, limit=20):
    """Public-facing issue index. Legacy digests are intentionally excluded."""
    rows = conn.execute(
        """SELECT si.*, count(sii.candidate_id) AS item_count
           FROM story_issues si
           LEFT JOIN story_issue_items sii ON sii.issue_id=si.id
           WHERE si.state='published'
           GROUP BY si.id ORDER BY si.published_at DESC, si.id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_story_review_queue(conn, limit=30):
    """Return candidate status plus its newest private draft, for review UI."""
    rows = conn.execute(
        """SELECT sc.*, count(scp.post_id) AS source_count,
                   d.id AS draft_id, d.headline AS draft_headline,
                   d.created_at AS draft_created_at
            FROM story_candidates sc
            LEFT JOIN story_candidate_posts scp ON scp.candidate_id=sc.id
            LEFT JOIN story_candidate_drafts d ON d.id=(
              SELECT d2.id FROM story_candidate_drafts d2
              WHERE d2.candidate_id=sc.id ORDER BY d2.created_at DESC, d2.id DESC LIMIT 1
            )
            GROUP BY sc.id
            ORDER BY CASE sc.state WHEN 'ready' THEN 0 WHEN 'watching' THEN 1 ELSE 2 END,
                     sc.urgency='breaking' DESC, sc.last_seen DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def get_timeline_window(conn, since_ts, until_ts):
    """Return unique posts in source-order, plus how often each appeared.

    The newest capture wins for display order. This is intentionally separate
    from ``get_posts_in_window``: a timeline edition represents what was seen,
    not merely everything whose source timestamp falls in the interval.
    """
    rows = conn.execute(
        """
        SELECT p.*, cp.position, tc.captured_at
        FROM capture_posts cp
        JOIN timeline_captures tc ON tc.id = cp.capture_id
        JOIN posts p ON p.id = cp.post_id
        -- A capture and a manually-triggered edition can share a wall-clock
        -- second; allow a tiny right-edge tolerance so the just-captured
        -- timeline is not mysteriously excluded.
        WHERE tc.captured_at >= ? AND tc.captured_at <= ?
        ORDER BY tc.captured_at DESC, cp.position ASC
        """, (since_ts, until_ts + 60)
    ).fetchall()
    out, seen = [], set()
    for row in rows:
        d = dict(row)
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        for field in ("media_urls", "link_urls", "metrics"):
            try:
                d[field] = json.loads(d[field]) if d.get(field) else []
            except (TypeError, json.JSONDecodeError):
                d[field] = []
        d["links"] = d.pop("link_urls")
        d["media"] = d.pop("media_urls")
        d["external_id"] = str(d["external_id"])
        out.append(d)
    return out, len(rows)


def save_edition(conn, digest_id, cards, clusters, ledger, research=None):
    """Store edition-specific presentation records alongside the digest."""
    conn.execute("UPDATE digests SET edition_meta=? WHERE id=?", (json.dumps(ledger), digest_id))
    for position, cluster in enumerate(clusters):
        conn.execute(
            "INSERT INTO edition_clusters (digest_id, position, label, post_ids, note) VALUES (?, ?, ?, ?, ?)",
            (digest_id, position, cluster["label"], json.dumps(cluster["post_ids"]), cluster.get("note")),
        )
    for position, card in enumerate(cards):
        conn.execute(
            "INSERT INTO edition_cards (digest_id, post_id, position, section, label, context_post_ids, context_posts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (digest_id, card["id"], position, card.get("section", "timeline"), card.get("label"),
             json.dumps(card.get("context_post_ids") or []), json.dumps(card.get("_edition_context") or [])),
        )
    if research:
        cur = conn.execute(
            "INSERT INTO research_briefs (digest_id, lead_label, body, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (digest_id, research["lead_label"], research["body"], research["status"], int(time.time())),
        )
        brief_id = cur.lastrowid
        for source in research.get("sources", []):
            conn.execute(
                "INSERT INTO research_sources (brief_id, url, title, excerpt, retrieved_at) VALUES (?, ?, ?, ?, ?)",
                (brief_id, source["url"], source.get("title"), source.get("excerpt"), int(time.time())),
            )


def get_edition(conn, digest_id):
    digest = get_digest_summary(conn, digest_id)
    if not digest:
        return None
    try:
        digest["edition_meta"] = json.loads(digest.get("edition_meta") or "{}")
    except (TypeError, json.JSONDecodeError):
        digest["edition_meta"] = {}
    cards = conn.execute(
        """SELECT ec.*, p.* FROM edition_cards ec JOIN posts p ON p.id=ec.post_id
           WHERE ec.digest_id=? ORDER BY ec.position""", (digest_id,)
    ).fetchall()
    digest["edition_cards"] = [dict(r) for r in cards]
    for card in digest["edition_cards"]:
        for field in ("metrics", "media_urls", "link_urls", "context_post_ids", "context_posts", "amplified_by"):
            if isinstance(card.get(field), str):
                try:
                    card[field] = json.loads(card[field])
                except (TypeError, json.JSONDecodeError):
                    card[field] = [] if field != "amplified_by" else None
    digest["edition_clusters"] = [dict(r) for r in conn.execute(
        "SELECT * FROM edition_clusters WHERE digest_id=? ORDER BY position", (digest_id,)
    ).fetchall()]
    brief = conn.execute("SELECT * FROM research_briefs WHERE digest_id=?", (digest_id,)).fetchone()
    if brief:
        digest["research_brief"] = dict(brief)
        digest["research_brief"]["sources"] = [dict(r) for r in conn.execute(
            "SELECT * FROM research_sources WHERE brief_id=? ORDER BY id", (brief["id"],)
        ).fetchall()]
    return digest


def upsert_digest_with_stories(conn, period_start, period_end, stories,
                               sources=None, model_versions=None,
                               zeitgeist=None, timeline_map=None,
                               input_post_ids=None, window_hours=None,
                               default_source="x"):
    """Write a digest row + all its stories + story_posts links in one transaction.

    stories: list of dicts each with: headline, story, topic, post_ids, post_count,
             total_score, plus optional key_quotes, media_references, cluster_method,
             theme_label, angle, prompt_tokens, completion_tokens, cost_usd.

    Two-stage digest metadata (zeitgeist + timeline_map) is JSON-encoded and written
    to the digest row for the UI to render alongside stories. (Phase 3.7.1 dropped
    debate_lines + signal_vs_noise; their DB columns remain for backward compat
    with old digests but are no longer written.)

    Returns the digest_id.
    """
    now = int(time.time())
    sources = sources or ["x"]
    topics = sorted({s.get("topic") for s in stories if s.get("topic")})
    model_versions = model_versions or {}

    cur = conn.execute("""
        INSERT INTO digests (
          period_start, period_end, generated_at,
          duration_estimate_minutes, story_count,
          topics, sources, model_versions,
          zeitgeist, debate_lines, signal_vs_noise, timeline_map,
          input_post_ids, window_hours
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        period_start, period_end, now,
        None,
        len(stories),
        json.dumps(topics),
        json.dumps(sources),
        json.dumps(model_versions),
        json.dumps(zeitgeist) if zeitgeist is not None else None,
        None,  # debate_lines — removed in Phase 3.7.1; column kept for back-compat
        None,  # signal_vs_noise — removed in Phase 3.7.1; column kept for back-compat
        json.dumps(timeline_map) if timeline_map is not None else None,
        json.dumps(input_post_ids) if input_post_ids is not None else None,
        window_hours,
    ))
    digest_id = cur.lastrowid

    for s in stories:
        cur = conn.execute("""
            INSERT INTO stories (
              digest_id, headline, story, key_quotes, media_references,
              topic, tags, coverage_type, continuity_story_id, what_changed,
              created_at, updated_at, post_count, total_score, cluster_method,
              prompt_tokens, completion_tokens, cost_usd,
              section, theme_label, angle
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            digest_id,
            s.get("headline", ""),
            s.get("story", ""),
            json.dumps(s.get("key_quotes") or []),
            json.dumps(s.get("media_references") or []),
            s.get("topic", ""),
            json.dumps(s.get("tags") or []),
            s.get("coverage_type", "one_shot"),
            s.get("continuity_story_id"),
            s.get("what_changed"),
            now, now,
            s.get("post_count", 0),
            s.get("total_score", 0.0),
            s.get("cluster_method", "embedding"),
            s.get("prompt_tokens"),
            s.get("completion_tokens"),
            s.get("cost_usd"),
            s.get("section", "story"),
            s.get("theme_label"),
            s.get("angle"),
        ))
        story_id = cur.lastrowid

        # Link posts to this story
        for post_id in s.get("post_ids", []):
            pid_row = conn.execute(
                "SELECT id FROM posts WHERE source = ? AND external_id = ?",
                (default_source, post_id),
            ).fetchone()
            if pid_row is None:
                continue
            conn.execute("""
                INSERT OR IGNORE INTO story_posts (story_id, post_id, relevance)
                VALUES (?, ?, 1.0)
            """, (story_id, pid_row["id"]))

    return digest_id


def upsert_digest_dossier(conn, digest_id, data):
    """Persist local-only pipeline diagnostics for a digest."""
    conn.execute("""
        INSERT INTO digest_dossiers (digest_id, data, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(digest_id) DO UPDATE SET data=excluded.data, created_at=excluded.created_at
    """, (digest_id, json.dumps(data, ensure_ascii=False), int(time.time())))


def get_digest_dossier(conn, digest_id):
    row = conn.execute("SELECT data FROM digest_dossiers WHERE digest_id=?", (digest_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except (TypeError, json.JSONDecodeError):
        return None


def list_digests(conn, limit=20, offset=0):
    """Return a list of digests ordered newest first. For the web UI index."""
    rows = conn.execute("""
        SELECT id, period_start, period_end, generated_at,
               duration_estimate_minutes, story_count,
               topics, sources, model_versions, window_hours, edition_meta, pipeline_run_id
        FROM digests
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in ("topics", "sources", "model_versions", "edition_meta"):
            v = d.get(f)
            if isinstance(v, str) and v:
                try:
                    d[f] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        out.append(d)
    return out


def list_pipeline_runs(conn, limit=20, step=None):
    """Return recent pipeline_runs. Optionally filter by step name."""
    if step:
        rows = conn.execute("""
            SELECT * FROM pipeline_runs WHERE step = ?
            ORDER BY id DESC LIMIT ?
        """, (step, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM pipeline_runs
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_posts_in_digest(conn, digest_id):
    """How many input candidate posts fed this digest (from input_post_ids)."""
    row = conn.execute("SELECT input_post_ids FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if not row or not row["input_post_ids"]:
        return 0
    try:
        return len(json.loads(row["input_post_ids"]))
    except (json.JSONDecodeError, TypeError):
        return 0


def get_digest_summary(conn, digest_id):
    """Return a digest row + its stories + their post links. For UI / debug."""
    digest = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if not digest:
        return None
    out = dict(digest)
    # Deserialise the two-stage synthesis JSON fields
    for f in ("zeitgeist", "debate_lines", "signal_vs_noise", "timeline_map", "input_post_ids", "topics", "sources", "model_versions"):
        v = out.get(f)
        if isinstance(v, str) and v:
            try:
                out[f] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
    out["stories"] = []
    for s in conn.execute("SELECT * FROM stories WHERE digest_id = ? ORDER BY total_score DESC", (digest_id,)).fetchall():
        sd = dict(s)
        for f in ("key_quotes", "media_references"):
            if sd.get(f):
                try:
                    sd[f] = json.loads(sd[f])
                except (json.JSONDecodeError, TypeError):
                    sd[f] = []
        # Attach the source posts
        sps = conn.execute("""
            SELECT p.* FROM story_posts sp
            JOIN posts p ON p.id = sp.post_id
            WHERE sp.story_id = ?
        """, (s["id"],)).fetchall()
        sd["posts"] = [dict(p) for p in sps]
        out["stories"].append(sd)
    return out

# ---------- image_context cache (Phase 3.6.1) ----------

def upsert_image_context(conn, media_url, status, description=None, error=None, model=None):
    """Insert or update an image_context row keyed by media_url.

    Status must be 'success', 'failed', or 'skipped'. Description is the
    vision model's caption; only set when status='success'. Error message
    is stored in fetch_error when status='failed'. Model records which
    vision model produced the description (None if failed/skipped).

    ON CONFLICT DO UPDATE so re-ingest or vision-model upgrade overwrites
    previous rows cleanly. Description is only written when status='success'
    so we don't accidentally clear a good description with a later 'failed'
    attempt (callers should not store 'failed' for an already-described URL).
    """
    now_ts = int(time.time())
    conn.execute(
        f"""
        INSERT INTO image_context (
            media_url, fetched_at, fetch_status, fetch_error, description, model
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(media_url) DO UPDATE SET
            fetched_at   = excluded.fetched_at,
            fetch_status = excluded.fetch_status,
            fetch_error  = excluded.fetch_error,
            description  = {_protected_set_clause('image_context', 'description')},
            model        = {_protected_set_clause('image_context', 'model')}
        """,
        (media_url, now_ts, status, error, description, model),
    )


def get_image_descriptions(conn, media_urls):
    """Look up descriptions for a list of media URLs in one query.

    Returns: dict[media_url -> {"description": str, "fetch_status": str,
                                  "model": str|None, "fetched_at": int}].
    Missing URLs are simply not in the returned dict.
    """
    if not media_urls:
        return {}
    placeholders = ",".join("?" for _ in media_urls)
    rows = conn.execute(
        f"""
        SELECT media_url, fetch_status, fetch_error, description, model, fetched_at
        FROM image_context
        WHERE media_url IN ({placeholders})
        """,
        tuple(media_urls),
    ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        out[d.pop("media_url")] = d
    return out


def get_image_context_stats(conn):
    """Return counts of image_context rows by status. Useful for /debug."""
    rows = conn.execute(
        """
        SELECT fetch_status, COUNT(*) AS n
        FROM image_context
        GROUP BY fetch_status
        """
    ).fetchall()
    return {r["fetch_status"]: r["n"] for r in rows}


# ---------- link_context cache (Phase 3.6.2) ----------

def upsert_link_context(conn, canonical_url, status, content_text=None, error=None, content_bytes=None):
    """Insert or update a link_context row keyed by canonical_url.

    Status must be 'success' or 'failed'. content_text is the extracted
    article body and is only persisted when status='success'. Error message
    is stored in fetch_error when status='failed'. content_bytes records
    the original HTTP body size (diagnostic) regardless of status.

    ON CONFLICT DO UPDATE so re-fetching overwrites cleanly. content_text
    and content_chars are only clobbered when the new row is a success so
    a failed retry doesn't wipe a previously-stored good extraction.
    """
    now_ts = int(time.time())
    content_chars = len(content_text) if content_text else None
    conn.execute(
        f"""
        INSERT INTO link_context (
            canonical_url, fetched_at, fetch_status, fetch_error,
            content_text, content_chars, content_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_url) DO UPDATE SET
            fetched_at    = excluded.fetched_at,
            fetch_status  = excluded.fetch_status,
            fetch_error   = excluded.fetch_error,
            content_text  = {_protected_set_clause('link_context', 'content_text')},
            content_chars = {_protected_set_clause('link_context', 'content_chars')},
            content_bytes = excluded.content_bytes
        """,
        (canonical_url, now_ts, status, error, content_text, content_chars, content_bytes),
    )


def get_link_texts(conn, canonical_urls):
    """Look up extracted article texts for a list of canonical URLs in one query.

    Returns: dict[canonical_url -> {"fetch_status", "fetch_error",
                                     "content_text", "content_chars",
                                     "content_bytes", "fetched_at"}].
    Missing URLs are simply not in the returned dict.
    """
    if not canonical_urls:
        return {}
    placeholders = ",".join("?" for _ in canonical_urls)
    rows = conn.execute(
        f"""
        SELECT canonical_url, fetch_status, fetch_error,
               content_text, content_chars, content_bytes, fetched_at
        FROM link_context
        WHERE canonical_url IN ({placeholders})
        """,
        tuple(canonical_urls),
    ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        out[d.pop("canonical_url")] = d
    return out


def get_link_context_stats(conn):
    """Return counts of link_context rows by status. Useful for /debug."""
    rows = conn.execute(
        """
        SELECT fetch_status, COUNT(*) AS n
        FROM link_context
        GROUP BY fetch_status
        """
    ).fetchall()
    return {r["fetch_status"]: r["n"] for r in rows}
