"""Herald's DB layer: connection helper, schema init, upserts, pipeline-run logging."""
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path


DB_PATH = Path(os.environ.get("HERALD_DB_PATH", "/var/lib/herald/herald.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


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
          metrics, post_type,
          conversation_id, quoted_tweet_id, in_reply_to_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, external_id) DO UPDATE SET
          author_id       = excluded.author_id,
          author_handle   = excluded.author_handle,
          text            = excluded.text,
          media_urls      = excluded.media_urls,
          link_urls       = excluded.link_urls,
          metrics         = excluded.metrics,
          post_type       = excluded.post_type,
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
               metrics, post_type,
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


def upsert_digest_with_stories(conn, period_start, period_end, stories,
                               sources=None, model_versions=None,
                               zeitgeist=None, debate_lines=None,
                               signal_vs_noise=None, timeline_map=None,
                               input_post_ids=None, window_hours=None):
    """Write a digest row + all its stories + story_posts links in one transaction.

    stories: list of dicts each with: headline, story, topic, post_ids, post_count,
             total_score, plus optional key_quotes, media_references, cluster_method,
             theme_label, angle, prompt_tokens, completion_tokens, cost_usd.

    Two-stage digest metadata (zeitgeist/debate_lines/signal_vs_noise/timeline_map)
    is JSON-encoded and written to the digest row for the UI to render alongside stories.

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
        json.dumps(debate_lines) if debate_lines is not None else None,
        json.dumps(signal_vs_noise) if signal_vs_noise is not None else None,
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
            s.get("cluster_method", "shared_link"),
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
                "SELECT id FROM posts WHERE source = 'x' AND external_id = ?",
                (post_id,),
            ).fetchone()
            if pid_row is None:
                continue
            conn.execute("""
                INSERT OR IGNORE INTO story_posts (story_id, post_id, relevance)
                VALUES (?, ?, 1.0)
            """, (story_id, pid_row["id"]))

    return digest_id


def list_digests(conn, limit=20, offset=0):
    """Return a list of digests ordered newest first. For the web UI index."""
    rows = conn.execute("""
        SELECT id, period_start, period_end, generated_at,
               duration_estimate_minutes, story_count,
               topics, sources, model_versions, window_hours
        FROM digests
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in ("topics", "sources", "model_versions"):
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
