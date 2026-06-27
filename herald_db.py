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
    """Open a SQLite connection to Herald's DB. Creates the parent dir if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn):
    """Apply schema.sql (idempotent: all CREATE statements use IF NOT EXISTS)."""
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
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
    mutable fields (author, text, media, etc.) and bumps ingested_at."""
    now = int(time.time())
    media  = json.dumps(post["media"])  if post.get("media")  is not None else None
    links  = json.dumps(post["links"])  if post.get("links")  is not None else None
    metrics = json.dumps(post["metrics"]) if post.get("metrics") is not None else None

    cur = conn.execute("""
        INSERT INTO posts (
          external_id, source, author_id, author_handle,
          created_at, ingested_at, text, media_urls, link_urls,
          metrics, post_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, external_id) DO UPDATE SET
          author_id     = excluded.author_id,
          author_handle = excluded.author_handle,
          text          = excluded.text,
          media_urls    = excluded.media_urls,
          link_urls     = excluded.link_urls,
          metrics       = excluded.metrics,
          post_type     = excluded.post_type,
          ingested_at   = excluded.ingested_at
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
    ))
    return cur.lastrowid


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


def finish_pipeline_run(conn, run_id, status, rows_out=None, error=None):
    """Mark a pipeline run complete with its final status and row counts."""
    now = int(time.time())
    conn.execute("""
        UPDATE pipeline_runs
        SET finished_at = ?, status = ?, rows_out = ?, error = ?
        WHERE id = ?
    """, (now, status, rows_out, error, run_id))


def get_posts_in_window(conn, since_ts, until_ts, source="x"):
    """Return posts where source matches and created_at is in [since_ts, until_ts).
    Each row is a dict matching the columns of `posts` (JSON fields deserialised)."""
    cur = conn.execute("""
        SELECT id, external_id, source, author_id, author_handle,
               created_at, ingested_at, text, media_urls, link_urls,
               metrics, post_type
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
        posts.append(d)
    return posts


def upsert_digest_with_stories(conn, period_start, period_end, stories, sources=None, model_versions=None):
    """Write a digest row + all its stories + story_posts links in one transaction.
    `stories` is a list of dicts each with: headline, story, topic, post_ids, post_count, total_score, plus optional key_quotes, media_references, cluster_method.

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
          topics, sources, model_versions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        period_start, period_end, now,
        None,  # duration_estimate_minutes; set later by an editor pass
        len(stories),
        json.dumps(topics),
        json.dumps(sources),
        json.dumps(model_versions),
    ))
    digest_id = cur.lastrowid

    for s in stories:
        cur = conn.execute("""
            INSERT INTO stories (
              digest_id, headline, story, key_quotes, media_references,
              topic, tags, coverage_type, continuity_story_id, what_changed,
              created_at, updated_at, post_count, total_score, cluster_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))
        story_id = cur.lastrowid

        # Link posts to this story
        for post_id in s.get("post_ids", []):
            # post_id here is the X tweet ID (the parser's "id" field); we
            # need to look up the internal `posts.id` PK from the DB.
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


def get_digest_summary(conn, digest_id):
    """Return a digest row + its stories + their post links. For UI / debug."""
    digest = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if not digest:
        return None
    out = dict(digest)
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
