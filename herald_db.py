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
