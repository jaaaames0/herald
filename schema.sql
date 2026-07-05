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

-- Conversation/threading columns. The parser emits these from bird's
-- conversationId / inReplyToStatusId / quotedTweet.id; they're persisted
-- so herald_context can look up cached thread context by conversation_id.
ALTER TABLE posts ADD COLUMN conversation_id TEXT;
ALTER TABLE posts ADD COLUMN quoted_tweet_id TEXT;
ALTER TABLE posts ADD COLUMN in_reply_to_id TEXT;

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
  rows_out INTEGER,
  -- Token / cost tracking (added 2026-06-28 for OpenRouter baseline).
  -- prompt_tokens + completion_tokens are summed across all LLM calls in this run.
  -- cost_usd is the OpenRouter-reported cost (sum of usage.cost across calls).
  -- NULL for non-LLM steps (collector, ingest, ranker, etc).
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost_usd REAL,
  model TEXT
);

-- Indices for common queries
CREATE INDEX IF NOT EXISTS idx_posts_created_at    ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author         ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_source         ON posts(source);
CREATE INDEX IF NOT EXISTS idx_posts_conversation   ON posts(conversation_id);
CREATE INDEX IF NOT EXISTS idx_posts_quoted         ON posts(quoted_tweet_id) WHERE quoted_tweet_id IS NOT NULL;
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
  cluster_method TEXT,                     -- 'shared_link', 'embedding', etc.
  -- Per-story token / cost tracking (added 2026-06-28 for OpenRouter baseline).
  prompt_tokens INTEGER,                   -- input tokens for this story's LLM call
  completion_tokens INTEGER,               -- output tokens (incl. reasoning_tokens if any)
  cost_usd REAL,                           -- OpenRouter-reported cost for this call
  -- Two-stage synthesis fields (added 2026-06-28 for timeline-aware digest).
  section TEXT DEFAULT 'story',             -- 'story' | 'zeitgeist' | 'debate' | 'signal_noise' (v1: always 'story')
  theme_label TEXT,                        -- which zeitgeist theme this story belongs to
  angle TEXT                               -- framing angle suggested by stage-1 timeline map
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
  model_versions TEXT,                     -- JSON: which model(s) produced each story
  -- Two-stage synthesis outputs (added 2026-06-28 for timeline-aware digest).
  zeitgeist TEXT,                          -- JSON: array of theme objects from stage 1
  debate_lines TEXT,                       -- JSON: array of debate objects from stage 1
  signal_vs_noise TEXT,                    -- JSON: {high_signal_themes, noise_patterns} from stage 1
  timeline_map TEXT,                       -- JSON: full stage-1 output (for debugging / re-runs)
  input_post_ids TEXT,                     -- JSON: array of post IDs that were candidates for this digest
  window_hours REAL                        -- size of the input window in hours (for transparency)
);

CREATE INDEX IF NOT EXISTS idx_stories_digest       ON stories(digest_id);
CREATE INDEX IF NOT EXISTS idx_stories_topic        ON stories(topic);
CREATE INDEX IF NOT EXISTS idx_digests_period       ON digests(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_story_posts_post     ON story_posts(post_id);

-- Idempotent column additions for token/cost tracking (added 2026-06-28)
-- and conversation context (added 2026-07-03).
-- SQLite ALTER TABLE ADD COLUMN has no IF NOT EXISTS; herald_db.init_schema
-- guards each ALTER with PRAGMA table_info so re-running schema.sql is a no-op.

-- pipeline_runs columns
ALTER TABLE pipeline_runs ADD COLUMN prompt_tokens INTEGER;
ALTER TABLE pipeline_runs ADD COLUMN completion_tokens INTEGER;
ALTER TABLE pipeline_runs ADD COLUMN cost_usd REAL;
ALTER TABLE pipeline_runs ADD COLUMN model TEXT;

-- stories columns
ALTER TABLE stories ADD COLUMN prompt_tokens INTEGER;
ALTER TABLE stories ADD COLUMN completion_tokens INTEGER;
ALTER TABLE stories ADD COLUMN cost_usd REAL;
ALTER TABLE stories ADD COLUMN section TEXT DEFAULT 'story';
ALTER TABLE stories ADD COLUMN theme_label TEXT;
ALTER TABLE stories ADD COLUMN angle TEXT;

-- digests columns (two-stage synthesis outputs)
ALTER TABLE digests ADD COLUMN zeitgeist TEXT;
ALTER TABLE digests ADD COLUMN debate_lines TEXT;
ALTER TABLE digests ADD COLUMN signal_vs_noise TEXT;
ALTER TABLE digests ADD COLUMN timeline_map TEXT;
ALTER TABLE digests ADD COLUMN input_post_ids TEXT;
ALTER TABLE digests ADD COLUMN window_hours REAL;

-- Context cache tables (added 2026-07-03, Phase 3 parser v2).
-- conversation_context holds the full parsed thread for a conversation_id,
-- fetched eagerly during collect and looked up during digest synthesis.
-- One row per conversation, regardless of how many posts reference it.
CREATE TABLE IF NOT EXISTS conversation_context (
  conversation_id TEXT PRIMARY KEY,
  fetched_at      INTEGER NOT NULL,
  fetch_status    TEXT NOT NULL,       -- 'success' | 'failed'
  fetch_error     TEXT,                -- error message if fetch_status='failed'
  thread_json     TEXT NOT NULL,       -- JSON array of parsed tweets from bird thread
  tweet_count     INTEGER NOT NULL,    -- length of thread_json array
  source_tweet_id TEXT                 -- which tweet of ours triggered the fetch (for debugging)
);

CREATE INDEX IF NOT EXISTS idx_conv_context_status ON conversation_context(fetch_status);

-- quote_context holds a single fetched tweet (the parent of a quote-tweet)
-- when bird's --quote-depth was insufficient to inline it. Separate from
-- conversation_context because a quote-tweet parent can live in any thread.
CREATE TABLE IF NOT EXISTS quote_context (
  quoted_tweet_id TEXT PRIMARY KEY,
  fetched_at      INTEGER NOT NULL,
  fetch_status    TEXT NOT NULL,       -- 'success' | 'failed'
  fetch_error     TEXT,
  data            TEXT NOT NULL        -- JSON: one parsed tweet
);

CREATE INDEX IF NOT EXISTS idx_quote_context_status ON quote_context(fetch_status);

-- Image context cache (Phase 3.6.1, added 2026-07-05).
-- Mirrors quote_context structure: one row per media URL, whether the URL
-- was successfully described by a vision LLM or the fetch failed.
-- media_url is the pbs.twimg.com (or other Twitter-hosted) image URL —

-- Image context cache (Phase 3.6.1, added 2026-07-05).
-- Mirrors quote_context structure: one row per media URL, whether the URL
-- was successfully described by a vision LLM or the fetch failed.
-- media_url is the pbs.twimg.com (or other Twitter-hosted) image URL,
-- stable, content-addressable in spirit. Once described, never refetched
-- unless the model changes (see model column).
CREATE TABLE IF NOT EXISTS image_context (
  media_url     TEXT PRIMARY KEY,
  fetched_at    INTEGER NOT NULL,
  fetch_status  TEXT NOT NULL,
  fetch_error   TEXT,
  description   TEXT,
  model         TEXT
);

CREATE INDEX IF NOT EXISTS idx_image_context_status ON image_context(fetch_status);
