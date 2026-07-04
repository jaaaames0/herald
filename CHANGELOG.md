# Herald — Changelog

Newest first. Dates in AEST (Australia/Sydney, GMT+10) unless noted.

---

## 2026-07-04 — Phase 3.5.1: recursive quote+reply chain unraveling

Builds on Phase 3.5. The previous version followed only the QUOTE dimension.
When a quote target was itself a reply (very common on X — people quote-tweet
other replies constantly), we'd walk the reply target's quotedTweet chain but
skip the reply target's reply parent. For chains that interleave quotes and
replies (e.g. "Big if true" → reply → quote of reply → reply → quote → quote →
source), we'd miss most of the chain.

Example: tweet 2073288532110557675 ("Big if true" by @xwanyex) is a 7-tweet
chain alternating quote and reply dimensions. Before 3.5.1, the LLM saw only
ONE tweet (the immediate quote target). After 3.5.1, the LLM sees all 7.

Three pieces:

- **`parser.parse_quoted` propagates `conversation` metadata for the quoted
  tweet** (`id`, `parent_id`, `is_reply`). Bird exposes `inReplyToStatusId`
  and `conversationId` on the inline JSON, but the parser was dropping them.
  Without this, the walker doesn't know the quoted tweet is a reply.
- **`herald_context._expand_context_chain`** now walks both quote and reply
  dimensions orthogonally. At each quote target reached via the chain, if
  the fetched tweet is itself a reply, also fetch its reply parent (and
  recurse on its quote chain). Quote depth and reply depth decrement
  independently — neither dimension blocks the other.
- **`herald_context.attach_context_to_candidates`** now emits
  `_quote_context` as a list of `{"tweet": ..., "_chain_kind": "quote"|"reply"}`
  items, ordered by DFS traversal. The synthesise prompt renders quote
  chain levels with `↳` and reply-parent edges with `⤴` so the LLM can
  tell the two dimensions apart in tangled chains.

**New env knobs:**
- `HERALD_QUOTE_DEPTH` (default bumped 3 → **5**) — quote chain depth per chain
- `HERALD_REPLY_PARENT_DEPTH` (default **2**) — reply chain depth per quote target
- `HERALD_FOLLOW_REPLY_PARENTS` (default **True**) — master toggle for reply chain

**Cost profile (per chain):**
- 1 quote + 0 reply (common): 0 fetches (inline suffices)
- 4 quotes + 0 reply: 3 fetches
- 4 quotes + 2 reply (James's pattern): 5 fetches
- Worst case (5 quote + 2 reply): 7 fetches per chain
- With `HERALD_CONTEXT_MAX_FETCHES=30`: ~4 such chains per collect before
  the budget starts trimming the deepest fetches

**Provenance in `quote_context.fetch_error`:**
- `inline-from-bird` — depth-1 seeded by parser at ingest (unchanged)
- `fetched-via-quote-chain` — fetched because something quote-tweeted this
- `fetched-via-reply-chain` — fetched because something replied to this

**Cost estimator (`herald_context.estimate_context_cost`)**: new helper
that walks the cache (or inline data) and returns:
- `quote_posts`: candidates with quote chains
- `fetch_attempts`: how many bird reads would happen (capped at budget)
- `context_tweets_attached`: total context items
- `approx_input_tokens`: rough context token estimate (~120 per item)

For the future admin panel — just call this with parsed_posts and display
the numbers. Useful for "with these settings, how much will this batch
cost?" and for tuning `HERALD_QUOTE_DEPTH` vs `HERALD_REPLY_PARENT_DEPTH`.

**Tests:** `test_parser_v2.py` extended from 12 to 17 cases:
- 13: enrich_batch unravels the full quote-of-reply-of-quote chain
- 14: attach_context emits interleaved quote/reply chain with kinds
- 15: rendering shows `⤴` for reply edges and `↳` for quote depth
- 16: `HERALD_FOLLOW_REPLY_PARENTS=0` collapses reply chain (toggle works)
- 17: `estimate_context_cost` returns sensible numbers (with-cache and fresh)

All pass. `test_mock_bird` extended with quote-of-reply fixtures
(`2000000000000001`–`2000000000000007`) and `FAIL_*` id prefix for
forced-failure paths.

**Live verification:** ingested tweet `2073288532110557675` on optiplex:
- Before 3.5.1: LLM sees only the inline quote target (1 tweet)
- After 3.5.1: LLM sees all 7 tweets, including the source @strangerous10
  tweet ("David Pocock pauses..."), with `↳` and `⤴` markers showing the
  quote/reply structure

**Files touched:** `parser.py`, `herald_context.py`, `herald_synthesise.py`,
`test_mock_bird`, `test_parser_v2.py`.

---

## 2026-07-04 — Phase 3.5: quote-context fix + recursive quote chains

Found a quiet bug while reviewing the quote-tweet path: the parser's
inline quote data (text + author + metrics, supplied by bird's
`--quote-depth 1`) was being silently dropped at ingest and never
surfaced to the LLM. The bug affected the common case — only deeply
nested quotes where bird couldn't inline made it through to the prompt.

Fix is in three pieces, with a recursive quote-chain walker on top:

- **`parser.parse_quoted` recurses one level** — when bird's JSON
  includes a nested `quotedTweet`, emit it as a stub (`id`, `url`, no
  text) inside the parsed quoted_tweet block. Previously parse_quoted
  only emitted the top-level fields, hiding the chain shape.
- **`herald_db.upsert_post` seeds `quote_context`** from inline data on
  ingest. `fetch_status='success'` with `fetch_error='inline-from-bird'`
  carries the provenance. Single source of truth for inline + fetched
  quotes, no special-case in the digest-time lookup.
- **`herald_context._expand_quote_chain`** walks the chain recursively
  up to `HERALD_QUOTE_DEPTH` (default 3). Inline data is a free cache
  hit; deeper levels are fetched via `bird read` and cached. Shares the
  `HERALD_CONTEXT_MAX_FETCHES` budget with thread fetches.
- **`herald_context.attach_context_to_candidates`** now emits
  `_quote_context` as a **list** of dicts (shallowest-to-deepest)
  instead of a single dict. BFS through `quote_context` loads every
  level reachable from the candidate's direct quote, so the walker
  doesn't re-query the DB per level.
- **`herald_synthesise._render_context_block`** renders nested levels
  with `↳` indentation so the LLM sees the chain structure. Thread
  members stay flat (depth-0). Header text updated to mention the
  `↳` marker convention.
- **`herald_db.get_connection` switches to WAL journal mode**
  (`PRAGMA journal_mode = WAL`, 10s timeout). Concurrent connections
  (collect + digest on overlapping schedules; test code that opens
  multiple connections) no longer block on the rollback journal.

**Cost profile (per collect):**
- Common case (depth-1 inlined): 0 bird reads (inline seed).
- 2-level chain: 1 bird read.
- 3-level chain: 2 bird reads.

**Tunables added:**
- `HERALD_QUOTE_DEPTH` (default 3): max chain levels

**Tests** — `test_parser_v2.py` extended from 7 to 12 cases:
  8. Inline quote is seeded by `upsert_post`
  9. `enrich_batch` chases the quote chain to depth 3
  10. `attach_context_to_candidates` walks the full chain into
      `_quote_context` list
  11. Synthesise prompt renders quote-chain depth with `↳` markers
  12. Failed quote fetch is recorded (`fetch_status='failed'`) and
      doesn't abort the batch
- `test_mock_bird` extended with `FAIL_*` id prefix that simulates a
  Twitter not-found (rc=3), plus fixtures for the depth-1 / depth-2 /
  depth-3 chain (`1000000000000001` → `1000000000000002` → `1000000000000003`).

**Verified end-to-end** on optiplex via dry-run digest:
  Before fix: "context attached: 8 threads, 0 quote parents"
  After fix:  "context attached: 4 threads, 7 quote parents" (4h window)
  `quote_context` now has 27 rows: 21 inline-seeded + 6 fetched for deeper levels.

**Files touched** — `parser.py`, `herald_db.py`, `herald_context.py`,
`herald_synthesise.py`, `test_mock_bird`, `test_parser_v2.py`,
`schema.sql` (comment-only), `ROADMAP.md`.

---

## 2026-07-03 — Doc split
- Split `docs/project/projects/planned/herald.md` (656 lines / ~50KB) into
  four files in `/home/james/srv/herald/`:
  - `README.md` — top-level orientation, status, quick start
  - `DESIGN.md` — design spec (architecture, stack, schema, source-grounding, decisions)
  - `ROADMAP.md` — phases 0–6, current status, next item
  - `CHANGELOG.md` — this file
- Old single file removed from `docs/project/projects/planned/`
- ROADMAP Phase 3 added: **Parser v2 — parent tweets & thread expansion**
  (next active item)
- ROADMAP Phase 5 added: **Accumulation-based publishing** (deferred until
  after multi-source; configurable threshold + publication-mode toggle)
- Design spec reflects current implementation: `gpt-oss-120b` reasoning
  model, embedding threshold 0.50, two-stage synthesis, source-agnostic
  schema, 16h/8h digest windows

---

## 2026-06-29 — Two-stage timeline-aware synthesis (live)
Per-cluster summarise was regurgitation; replaced with editorial reasoning
across the whole timeline. Per James: "its not enough to just distill each
tweet individually, its about taking the whole timeline and drawing
conclusions across it about whats newsworthy, whats trending, where the
discussion is at and where the signal lies."

- **Stage 1 LLM call** (`herald_synthesise.stage1_timeline_map`): sees all
  ~40 candidates. Outputs:
  - `zeitgeist` — themes with salience ratings
  - `debate_lines` — named accounts on each side of a debate
  - `signal_vs_noise` — signal-vs-noise patterns
  - `stories_to_write` — list of story themes with framing angles
- **Stage 2 LLM call** (`herald_synthesise.stage2_story`): one per story
  (max 5), sees only its theme's posts. Writes 1–3 paragraph narrative
  with inline `[post:ID]` citations, embedded quotes with `@author`
  attribution.
- Source-grounding rules are HARD constraints in the prompt.
- Schema additions: `digests.zeitgeist`, `debate_lines`, `signal_vs_noise`,
  `timeline_map`, `input_post_ids`, `window_hours`. `stories.theme_label`,
  `angle`, `section`.
- Reasoning model (`gpt-oss-120b`) is the right call here. Stage 1 needs
  to find themes ACROSS posts that don't share links or words — that's
  pattern recognition + editorial judgement.

---

## 2026-06-28 — Embedding pre-filter (live)
- `herald_embed.py` — embed all posts in window via `text-embedding-3-small`
  (OpenRouter)
- Greedy cluster by cosine similarity, threshold 0.50 (tuned against real
  X data — 0.65 was too strict, produced 0 multi-post clusters; 0.50
  produces ~3 multi-post clusters per 41 posts)
- Rank clusters by `size × engagement × log10(followers+10)`
- Pick ~40 representative candidates (cluster centroid + 1–2 high-engagement
  members)
- Pre-filter is critical: keeps each LLM call's context bounded (~5k input
  for stage 1, ~1k for stage 2), so we can have ~40 candidates in stage 1
  without context explosion.

---

## 2026-06-28 — First automated digest (live)
- 06:00 UTC ingest run: 183 fresh posts, 207 total in DB
- First digest scheduled for 20:00 UTC (06:00 AEST 2026-06-29)
- Web UI live at `herald.jaaaames.com`, TLS via Let's Encrypt (cert
  expanded to include SAN)
- Flask + gunicorn on 127.0.0.1:8060, reverse-proxied by nginx
- Bootstrap-free dark theme: `#0d1117` bg, `#34d399` accent, monospace
  stack, sidebar layout
- Routes: `/`, `/digest/<id>`, `/debug`. No auth (single user, public data).
- Cert now covers all 11 jaaaames.com subdomains; expires 2026-09-26

---

## 2026-06-28 — Cost baseline (Phase 0)
- `gpt-oss-120b` chosen: $0.03/M input + $0.15/M output (OpenRouter)
- Stage 1 typical: 4978 input + 3407 output (mostly reasoning_tokens)
- Stage 2 typical: ~900 input + ~900 output per story
- Per-call budget `HERALD_STAGE{1,2}_MAX_TOKENS` = 64000 to give reasoning
  room
- Cheaper non-reasoning alternatives if cost matters:
  `mistralai/mistral-nemo` ($0.02/$0.03), `meta-llama/llama-3.1-8b-instruct`
  ($0.02/$0.03), `google/gemma-3-4b-it` ($0.05/$0.10)

---

## 2026-06-27 — Parser fixes (Phase 0)
Forked `bird_parse.py` from newsroom iteration. Three fixes:

1. **parse_media**: extract `media_url_https` from
   `legacy.extended_entities.media[].media_url_https` (was nulled out —
   Herald's image requirement couldn't be met otherwise)
2. **parse_conversation**: emit a STUB parent_tweet (id + url, `text=null`)
   — bird `--json-full` does NOT inline parent content. Future Phase 3
   (parser v2) will fetch full parent text on demand.
3. **get_tweet_text**: prefer `note_tweet` (long-form) text over outer
   `text` (which may be truncated for >280-char posts)

Plus: `post_type` uses X-familiar vocabulary ("retweet", not generic
"repost") — Herald is X-first. Future source collectors can map other
platforms' concepts at the boundary.

---

## 2026-06-27 — Schema (Phase 0)
- Source-agnostic: `posts.source` defaults to `'x'`, `post_type` ∈
  {original, reply, repost, quote}
- Tables: `posts`, `accounts`, `pipeline_runs` (Phase 0); `stories`,
  `story_posts`, `digests` (added with synthesis)
- Indexes on common queries: posts by `created_at`, `author`, `source`;
  `pipeline_runs` by `(step, started_at)`; stories by `digest_id`,
  `topic`; digests by `(period_start, period_end)`; `story_posts` by
  `post_id`
- Idempotent `ALTER TABLE` migrations for token/cost tracking and
  two-stage synthesis columns, applied via `schema.sql` re-runs

---

## 2026-06-27 — Manual digest CLI (Phase 0)
- `herald_digest.py digest --since "12h ago" --top 5 --max-candidates 40`
- Pipeline: pull posts → cluster heuristically (shared links + time
  window + author overlap) → rank → summarise via OpenRouter → assemble
  digest
- Validates source-grounding (every `[post:ID]` in story text maps to a
  real input post)
- Falls back to extractive digest if validation fails
- `--dry-run` mode skips LLM calls (placeholder synthesis, no cost) for
  pipeline testing

---

## 2026-06-27 — Design phase 4 (cull)
Major cull. Every removed feature tested against "does this add value
proportional to the moving parts it adds?"

- **Feedback system: removed entirely.** Per-account weights,
  interest/noise, snooze UI — gone. Hand-tuned ranker with
  `log(follower_count)` is the lever.
- **Email: removed.** Web UI is the only surface.
- **Watchdog: removed.** Debug log + `pipeline_runs` is enough.
- **Status page: replaced with debug log.**
- **Hosting: optiplex from Phase 0**, not chicken→optiplex migration later.
- **Source-agnostic from start.** `posts.source` field, generic
  `post_type`.
- **Phasing consolidated.** 0 (foundation) → 1 (automation + UI) → 2
  (synthesis) → 3 (multi-source) → 4 (deep dives, stretch).
- **Schema trimmed.** `feedback`, `interactions`, `watchdog_state` tables
  dropped. `accounts.weight`, `accounts.muted` dropped.
- **Decisions Made section rewritten** to reflect the leaner design.
- **Open Questions section rewritten.** Removed email/feedback open
  questions; added bird-auth-on-optiplex and ranker-normalisation-formula
  questions.

---

## 2026-06-27 — Design phase 3
- Digest reframed as **self-contained narrative product**, not gateway to
  X. Per James: the digest itself contains the 15 minutes of reading;
  reader should not need to click through to X. Source tweets are cited
  for verification, but the narrative lives in the digest.
- Scope tightened: 5–10 substantive stories per digest, not 20–40. Each
  story is a multi-paragraph narrative synthesis (1–3 paragraphs) with
  editorial framing, embedded quotes, source citations.
- LLM output format changed to support self-contained narrative:
  `story` field is prose with inline `[post:ID]` citations; `key_quotes`
  tracks quotes embedded in the narrative; validation operates on the
  prose directly.
- **Continuity made structural.** Added `coverage_type`
  (`one_shot` | `ongoing`), `tags` (JSON), `continuity_story_id`,
  `what_changed` to the stories schema. Replaces the prior iteration's
  ad-hoc "Stories still developing" lists.
- Studied prior iteration
  (`~/tmp/openclaw-backup/workspace/projects/newsroom/`), especially
  `SYSTEM.md`, `schema.md`, `bird_parse.py`, `bird_context.py`. Key
  finding: previous failure was caused by OpenClaw cron jobs driving the
  agent via batch synthesis — confirmed "maxConcurrentRuns too high →
  AI service temporarily overloaded" failure mode. Herald's LLM
  architecture (no agent in the synthesis path) directly addresses this.
- Inherited parser (`bird_parse.py`) and identified gaps to fix in
  Phase 0: media URLs nulled out, `parent_tweet.text` echoes reply text
  (bug), `note_tweet` long-form not extracted. RT amplification signal
  discarded by design — accepted as design call (rely on `retweetCount`).
- **First Phase 0 task: `bird` auth.** Cookies not currently set on
  chicken; previous `credentials.env` is empty. Need `auth_token` + `ct0`
  cookies (via env vars, browser extraction, or flags).
- Test fixtures identified. Existing feed files in
  `~/tmp/openclaw-backup/workspace/projects/newsroom/feed/` (~12 JSONL
  files, ~36 hours of X data) can drive Phase 0 development without
  needing live `bird` auth.
- Reference sample digest.
  `~/tmp/openclaw-backup/workspace/projects/newsroom/archive/2026-05-13_0600_digest.md`
  is the model for the Herald digest format.

---

## 2026-06-27 — Design phase 2
- Project crystallised: moved from
  `horizon/ideas/x-feed-digest.md` → `projects/planned/herald.md`.
- Collector mechanism resolved: build on `bird` CLI + existing JSON
  parser. Not a rebuild.
- Schedule resolved: 6am AEST + 2pm AEST (framing against US Eastern
  hours).
- Naming resolved: Herald (medieval emissary / town crier).
- **LLM architecture resolved: Herald does NOT route through the agent's
  context window.** Previous attempts failed by trying to make the
  agent produce digests, which overloaded context. Herald has its own
  batch pipeline calling OpenRouter directly. The agent may *interact
  with* Herald (read digests, give feedback) but does not *run*
  synthesis.
- OpenRouter chosen as the LLM provider. Model-swappable. Start cheap,
  upgrade experimentally.
- Embedding model considered for clustering. Adopted for Phase 2+.
- Multi-source: X is commentary layer; news baseline is a later
  architectural goal (Phase 4). Designed in but deferred.
- Source-grounding: made a hard constraint. Every claim must cite a
  source post; LLM output is structured JSON; validation pipeline
  enforces it; UI shows inline citations + images. The previous
  attempts' hallucination failure is the primary motivator.
- Watchdog removed in favour of structured logging + status page. (Phase
  4 then removed the status page too — debug log is the final visibility
  mechanism.)
- Retention: 90 days default for raw posts; digests and accounts kept
  forever.
- Story continuity: Phase 3 stretch goal; cost trade-off acknowledged.
- Phasing redrawn around "automate the easy parts last" — Phase 0 is
  bird + parser + manual digest, not a hypothetical manual-mode
  validation gate.

---

## 2026-06-27 — Design phase 1
- Discussed goal: twice-daily 15-min X-feed digest.
- Two prior attempts failed by "lacking consistency in producing an
  ongoing digest" — design intentionally agnostic of those systems,
  addresses failure mode through idempotency, observability, graceful
  degradation, structured logging, no watchdog.
- Working name selected: **Herald**.
- Herald = "a medieval emissary or town crier; one who reads the news
  aloud"
## 2026-07-04 — Phase 3.5.1: recursive quote+reply chain unraveling

Builds on Phase 3.5. The previous version followed only the QUOTE dimension.
When a quote target was itself a reply (very common on X — people quote-tweet
other replies constantly), we'd walk the reply target's quotedTweet chain but
skip the reply target's reply parent. For chains that interleave quotes and
replies (e.g. "Big if true" → reply → quote of reply → reply → quote → quote →
source), we'd miss most of the chain.

Example: tweet 2073288532110557675 ("Big if true" by @xwanyex) is a 7-tweet
chain alternating quote and reply dimensions. Before 3.5.1, the LLM saw only
ONE tweet (the immediate quote target). After 3.5.1, the LLM sees all 7.

Three pieces:

- **`parser.parse_quoted` propagates `conversation` metadata for the quoted
  tweet** (`id`, `parent_id`, `is_reply`). Bird exposes `inReplyToStatusId`
  and `conversationId` on the inline JSON, but the parser was dropping them.
  Without this, the walker doesn't know the quoted tweet is a reply.
- **`herald_context._expand_context_chain`** now walks both quote and reply
  dimensions orthogonally. At each quote target reached via the chain, if
  the fetched tweet is itself a reply, also fetch its reply parent (and
  recurse on its quote chain). Quote depth and reply depth decrement
  independently — neither dimension blocks the other.
- **`herald_context.attach_context_to_candidates`** now emits
  `_quote_context` as a list of `{"tweet": ..., "_chain_kind": "quote"|"reply"}`
  items, ordered by DFS traversal. The synthesise prompt renders quote
  chain levels with `↳` and reply-parent edges with `⤴` so the LLM can
  tell the two dimensions apart in tangled chains.

**New env knobs:**
- `HERALD_QUOTE_DEPTH` (default bumped 3 → **5**) — quote chain depth per chain
- `HERALD_REPLY_PARENT_DEPTH` (default **2**) — reply chain depth per quote target
- `HERALD_FOLLOW_REPLY_PARENTS` (default **True**) — master toggle for reply chain

**Cost profile (per chain):**
- 1 quote + 0 reply (common): 0 fetches (inline suffices)
- 4 quotes + 0 reply: 3 fetches
- 4 quotes + 2 reply (James's pattern): 5 fetches
- Worst case (5 quote + 2 reply): 7 fetches per chain
- With `HERALD_CONTEXT_MAX_FETCHES=30`: ~4 such chains per collect before
  the budget starts trimming the deepest fetches

**Provenance in `quote_context.fetch_error`:**
- `inline-from-bird` — depth-1 seeded by parser at ingest (unchanged)
- `fetched-via-quote-chain` — fetched because something quote-tweeted this
- `fetched-via-reply-chain` — fetched because something replied to this

**Cost estimator (`herald_context.estimate_context_cost`)**: new helper
that walks the cache (or inline data) and returns:
- `quote_posts`: candidates with quote chains
- `fetch_attempts`: how many bird reads would happen (capped at budget)
- `context_tweets_attached`: total context items
- `approx_input_tokens`: rough context token estimate (~120 per item)

For the future admin panel — just call this with parsed_posts and display
the numbers. Useful for "with these settings, how much will this batch
cost?" and for tuning `HERALD_QUOTE_DEPTH` vs `HERALD_REPLY_PARENT_DEPTH`.

**Tests:** `test_parser_v2.py` extended from 12 to 17 cases:
- 13: enrich_batch unravels the full quote-of-reply-of-quote chain
- 14: attach_context emits interleaved quote/reply chain with kinds
- 15: rendering shows `⤴` for reply edges and `↳` for quote depth
- 16: `HERALD_FOLLOW_REPLY_PARENTS=0` collapses reply chain (toggle works)
- 17: `estimate_context_cost` returns sensible numbers (with-cache and fresh)

All pass. `test_mock_bird` extended with quote-of-reply fixtures
(`2000000000000001`–`2000000000000007`) and `FAIL_*` id prefix for
forced-failure paths.

**Live verification:** ingested tweet `2073288532110557675` on optiplex:
- Before 3.5.1: LLM sees only the inline quote target (1 tweet)
- After 3.5.1: LLM sees all 7 tweets, including the source @strangerous10
  tweet ("David Pocock pauses..."), with `↳` and `⤴` markers showing the
  quote/reply structure

**Files touched:** `parser.py`, `herald_context.py`, `herald_synthesise.py`,
`test_mock_bird`, `test_parser_v2.py`.


## 2026-07-05 — Tests 18-20 (Phase 3.5.1 robustness)

Three more test cases for the recursive walker. All pass.

- **Test 18** (pure reply chain, no quotes): A→B→C→D with no `quotedTweet`
  anywhere. Walker follows reply edges until `HERALD_REPLY_PARENT_DEPTH`
  cap. Fixtures: `3000000000000001`–`3000000000000004` in `test_mock_bird`.
- **Test 19** (cycle protection): A↔B (A replies B, B replies A).
  `visited` set breaks the back-edge cleanly. Fixtures:
  `3100000000000001`–`3100000000000002`.
- **Test 20** (quote-depth cap): 8-level deep quote chain, walker fetches
  levels 2–5 (4 fetches) and stops at `HERALD_QUOTE_DEPTH=5`. Levels 6–8
  must NOT be cached. Fixtures: `4000000000000001`–`4000000000000008`.
  Inline level-1 fixture switched from raw bird shape (`quotedTweet`
  camelCase) to parsed shape (`quoted_tweet` snake_case) — the walker
  reads the parsed form after loading from cache.

`test_parser_v2.py` is now 20 tests, all green.
