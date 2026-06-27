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

-- Synthesis tables (added in Phase 0 step 5)
-- A digest is one press at one time. It contains multiple stories. Each
-- story references multiple posts via story_posts. Clusters are in-memory
-- only; the cluster_method column records which algorithm produced the cluster.

CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  digest_id INTEGER,                       -- NULL until assigned to a digest
  headline TEXT,
  story TEXT,                              -- multi-paragraph narrative; [post:ID] inline citations
  key_quotes TEXT,                         -- JSON: array of {post_id, quote, speaker, used_in_story}
  media_references TEXT,                   -- JSON: array of {post_id, url, description, used_in_story}
  topic TEXT,
  tags TEXT,                               -- JSON array
  coverage_type TEXT DEFAULT 'one_shot',    -- 'one_shot' | 'ongoing'
  continuity_story_id INTEGER,             -- if this is a follow-up to an earlier story
  what_changed TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER,
  post_count INTEGER,
  total_score REAL,
  cluster_method TEXT                      -- 'shared_link', 'embedding', etc.
);

CREATE TABLE IF NOT EXISTS story_posts (
  story_id INTEGER NOT NULL,
  post_id INTEGER NOT NULL,
  relevance REAL DEFAULT 1.0,              -- 0-1; how central this post is to the story
  PRIMARY KEY (story_id, post_id)
);

CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_start INTEGER NOT NULL,
  period_end INTEGER NOT NULL,
  generated_at INTEGER NOT NULL,
  duration_estimate_minutes REAL,          -- pre-flight reading-time estimate
  story_count INTEGER,
  topics TEXT,                             -- JSON: what was in scope
  sources TEXT,                            -- JSON: which sources contributed posts
  model_versions TEXT                      -- JSON: which model(s) produced each story
);

CREATE INDEX IF NOT EXISTS idx_stories_digest       ON stories(digest_id);
CREATE INDEX IF NOT EXISTS idx_stories_topic        ON stories(topic);
CREATE INDEX IF NOT EXISTS idx_digests_period       ON digests(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_story_posts_post     ON story_posts(post_id);
