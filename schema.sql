-- Herald's source-agnostic schema. Applied idempotently by herald_db.init_schema().
--
-- For Phase 0 we use only posts, accounts, pipeline_runs.
-- stories, story_posts, digests are added when we get to the synthesis pipeline.

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  external_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'x',
  author_id TEXT NOT NULL,
  author_handle TEXT NOT NULL,
  created_at INTEGER NOT NULL,           -- source timestamp, Unix epoch
  ingested_at INTEGER NOT NULL,         -- herald's ingest time
  text TEXT,
  media_urls TEXT,                       -- JSON array
  link_urls TEXT,                        -- JSON array
  metrics TEXT,                          -- JSON: {likes, retweets, replies, ...}
  post_type TEXT,                        -- 'original' | 'reply' | 'repost' | 'quote'
  raw TEXT,                              -- full original payload (not populated in v1)
  UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'x',
  handle TEXT NOT NULL,
  display_name TEXT,
  bio TEXT,
  follower_count INTEGER,
  verified INTEGER DEFAULT 0,
  first_seen INTEGER,
  last_seen INTEGER,
  notes TEXT,
  PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  step TEXT NOT NULL,                    -- 'collector', 'clusterer', 'ranker', 'summarizer', 'assembler', 'ingest'
  source TEXT,                           -- 'x', 'rss', ... or NULL for source-agnostic steps
  status TEXT NOT NULL,                  -- 'running', 'success', 'failed', 'skipped'
  error TEXT,
  rows_in INTEGER,
  rows_out INTEGER
);

-- Indices for common queries
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author      ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_source      ON posts(source);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_step_time ON pipeline_runs(step, started_at);
