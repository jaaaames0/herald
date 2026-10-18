"""SQLite storage for Herald.

Five tables. Posts are stored whole as JSON (the normalised shape from
herald_x); a few columns are lifted out for window queries. Context-only rows
are posts fetched to complete a chain (a reply parent or a deeper quote) that
did not themselves appear in the Following feed.
"""

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("HERALD_DB_PATH", "/var/lib/herald/herald-feed.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  author TEXT NOT NULL,
  seen_at INTEGER,
  posted_at INTEGER,
  context_only INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_seen ON posts(context_only, seen_at);

CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_at INTEGER NOT NULL,
  cards INTEGER NOT NULL,
  posts INTEGER NOT NULL,
  new_posts INTEGER NOT NULL,
  oldest_seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS image_readings (
  url TEXT PRIMARY KEY,
  status TEXT NOT NULL,             -- 'ok' | 'failed'
  reading TEXT,                     -- JSON from the vision model
  model TEXT,
  cost_usd REAL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot TEXT NOT NULL,               -- 'morning' | 'afternoon' | 'manual'
  period_start INTEGER NOT NULL,
  period_end INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  model TEXT,
  cost_usd REAL,
  data TEXT NOT NULL                -- rendered-ready digest JSON
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  step TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  status TEXT NOT NULL DEFAULT 'running',
  detail TEXT,
  cost_usd REAL
);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_post(conn, post, context_only=False, now=None):
    """Insert or refresh a post. Returns True when the post is new.

    Refreshing keeps the earliest `seen_at`, unions `retweeted_by`, keeps any
    already-resolved quote chain, and promotes a context-only row to a feed
    row if it later appears in the feed.
    """
    now = now or int(time.time())
    row = conn.execute("SELECT data, context_only FROM posts WHERE id=?", (post["id"],)).fetchone()
    if row:
        old = json.loads(row["data"])
        if old.get("seen_at") and (not post.get("seen_at") or old["seen_at"] < post["seen_at"]):
            post["seen_at"] = old["seen_at"]
        post["retweeted_by"] = list(dict.fromkeys((old.get("retweeted_by") or []) + (post.get("retweeted_by") or [])))
        if old.get("quoted") and not post.get("quoted"):
            post["quoted"] = old["quoted"]
        context_only = bool(row["context_only"]) and context_only
    conn.execute(
        "INSERT INTO posts (id, author, seen_at, posted_at, context_only, data, updated_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET author=excluded.author, "
        "seen_at=excluded.seen_at, posted_at=excluded.posted_at, context_only=excluded.context_only, "
        "data=excluded.data, updated_at=excluded.updated_at",
        (post["id"], post["author"]["handle"], post.get("seen_at"), post.get("posted_at"),
         int(context_only), json.dumps(post, ensure_ascii=False), now))
    return row is None


def get_post(conn, pid):
    row = conn.execute("SELECT data FROM posts WHERE id=?", (str(pid),)).fetchone()
    return json.loads(row["data"]) if row else None


def posts_in_window(conn, start, end):
    rows = conn.execute(
        "SELECT data FROM posts WHERE context_only=0 AND seen_at>=? AND seen_at<? ORDER BY seen_at, id",
        (start, end)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def record_capture(conn, captured_at, cards, posts, new_posts, oldest_seen_at):
    conn.execute("INSERT INTO captures (captured_at, cards, posts, new_posts, oldest_seen_at) VALUES (?,?,?,?,?)",
                 (captured_at, cards, posts, new_posts, oldest_seen_at))


def get_reading(conn, url):
    row = conn.execute("SELECT status, reading FROM image_readings WHERE url=?", (url,)).fetchone()
    if not row:
        return None
    return {"status": row["status"], "reading": json.loads(row["reading"]) if row["reading"] else None}


def save_reading(conn, url, status, reading, model, cost):
    conn.execute(
        "INSERT OR REPLACE INTO image_readings (url, status, reading, model, cost_usd, created_at) VALUES (?,?,?,?,?,?)",
        (url, status, json.dumps(reading, ensure_ascii=False) if reading else None, model, cost, int(time.time())))


def save_digest(conn, slot, start, end, model, cost, data):
    cur = conn.execute(
        "INSERT INTO digests (slot, period_start, period_end, created_at, model, cost_usd, data) VALUES (?,?,?,?,?,?,?)",
        (slot, start, end, int(time.time()), model, cost, json.dumps(data, ensure_ascii=False)))
    return cur.lastrowid


def get_digest(conn, digest_id=None):
    if digest_id is None:
        row = conn.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT * FROM digests WHERE id=?", (digest_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["data"] = json.loads(d["data"])
    return d


def previous_digests(conn, before, limit=2):
    """The last scheduled editions that ended by `before` (manual and eval runs don't count)."""
    rows = conn.execute(
        "SELECT * FROM digests WHERE slot IN ('morning', 'afternoon') AND period_end <= ? "
        "ORDER BY period_end DESC, id DESC LIMIT ?", (before + 3600, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"])
        out.append(d)
    return out


def list_digests(conn, limit=60):
    return [dict(r) for r in conn.execute(
        "SELECT id, slot, period_start, period_end, created_at, model, cost_usd FROM digests ORDER BY id DESC LIMIT ?",
        (limit,))]


def start_run(conn, step):
    cur = conn.execute("INSERT INTO runs (step, started_at) VALUES (?,?)", (step, int(time.time())))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, detail=None, cost=None):
    conn.execute("UPDATE runs SET finished_at=?, status=?, detail=?, cost_usd=? WHERE id=?",
                 (int(time.time()), status, detail, cost, run_id))
    conn.commit()


def recent_runs(conn, limit=40):
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
