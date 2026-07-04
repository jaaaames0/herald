# Herald — Roadmap

Phases are ordered by current plan. Phase 0/1 are done; Phase 2 is partially
done; Phase 3 is the next active item.

| Phase | Status | What |
|---|---|---|
| 0 — Foundation | ✅ DONE | bird + parser + manual digest |
| 1 — Automation + Web UI | ✅ DONE | systemd timers, gunicorn, nginx, cert |
| 2 — Synthesis sophistication | 🟡 PARTIAL | embeddings ✅, two-stage ✅, continuity pending, ranker tuning pending, per-story prompts pending |
| 3 — Parser v2 | ✅ DONE (2026-07-04) | parent tweets of replies + thread expansion + recursive quote chains |
| 3.5 — Inline-quote fix + recursive quote chains | ✅ DONE (2026-07-04) | seed quote_context at ingest; chain walker to HERALD_QUOTE_DEPTH (default 3) |
| 4 — Multi-source | ⏭ PLANNED | RSS collector first, then news API |
| 5 — Accumulation-based publishing | ⏭ DEFERRED | depends on Phase 4 (multi-source) |
| 6 — Deep dives / context gathering | ⏭ STRETCH | for low-context / technical stories |

---

## Phase 3 — Parser v2 ✅ DONE (2026-07-04) + Phase 3.5 (same day)

**Goal:** Herald sees replies in full context, and quote-tweets in full
recursive context, not just the immediate post text.

### Phase 3 — replies ✅ (shipped 2026-07-03, kept)
Reply chains are followed end-to-end. `bird thread <conv_id> --json-full`
runs during `collect` and the parsed thread is cached in `conversation_context`.
At digest time, `attach_context_to_candidates` attaches the thread as
`_thread_context` on each reply candidate, and `_render_context_block`
surfaces those context posts in the LLM prompt.

### Phase 3.5 — quote-tweets ✅ (shipped 2026-07-04)
Quote-tweet chains are followed recursively. Bird's `--quote-depth 1` inlines
the immediate quoted parent in most cases, but the parser used to throw that
inline data away at ingest — a quiet bug that meant the LLM only saw
"Author X quote-tweeted [link]" with no content from the quoted tweet.

The fix has three parts:

1. **`parser.parse_quoted` recurses one level**, emitting a stub for the
   nested quoted tweet (id + url, no text) when bird's JSON includes one.
   This makes the chain shape visible to downstream code.

2. **`herald_db.upsert_post` seeds `quote_context`** from inline data on
   ingest (when `quoted_tweet.text` is present). Single source of truth
   for both inline and fetched quotes.

3. **`herald_context.enrich_batch` walks the chain** via `_expand_quote_chain`
   up to `HERALD_QUOTE_DEPTH` (default 3). Inline data is a free cache hit;
   deeper levels are fetched via `bird read <id> --json-full` and cached.
   `attach_context_to_candidates` then loads the full chain via BFS through
   `quote_context` and emits it as `_quote_context` (a list of dicts,
   shallowest-to-deepest).

**Cost profile:**
- Common case (depth-1 inlined): 0 bird reads (inline seed suffices).
- 2-level chain: 1 bird read (depth-2 fetch).
- 3-level chain: 2 bird reads.
- All depths share `HERALD_CONTEXT_MAX_FETCHES` budget with thread fetches.

**Verified by `test_parser_v2.py` (12 cases):** inline seed, chain walk
to depth 3, attach context emits list, prompt renders depth with ↳ markers,
failed fetches recorded as `failed` and don't abort batch.

**Verified end-to-end on optiplex:** the live dry-run digest now reports
"7 quote parents" attached for a 4h window (was 0 before the fix because
the cache was empty for inlined quotes).

**Env knobs added:**
- `HERALD_QUOTE_DEPTH` (default 3): max chain levels
- `HERALD_CONTEXT_MAX_FETCHES` (default 30, unchanged): shared budget

**Open items deferred:**
- Failed quote fetches don't auto-retry (`fetch_status='failed'` is sticky).
  Manual `DELETE FROM quote_context WHERE fetch_status='failed'` forces
  retry. Same shape as the conversation-context retry story; consolidate
  later if both get a "retry policy" rework.
- No depth cap on the thread fetch (some 30+ tweet threads may token-cost
  a lot). Same as before, still open.

---

## Phase 4 — Multi-source ⏭ PLANNED

---

## Phase 0 — Foundation ✅ DONE (2026-06-27)
**Goal:** Produce the first end-to-end Herald digest by hand. Validate the
synthesis pipeline before investing in scheduling or UI.

- [x] Set up `bird` on optiplex with auth (AUTH_TOKEN + CT0 from Twitter cookies)
- [x] Fork `bird_parse.py` into Herald's repo, fix the three gaps (media URLs, parent_tweet stub, note_tweet long-form)
- [x] Scaffold SQLite schema in `/var/lib/herald/herald.db`
- [x] Write ingest path: validate, dedupe by `(source, external_id)`, write to DB
- [x] Build manual `herald_digest.py digest --since "12h ago"` CLI: pull, cluster (heuristic), rank, summarise, write digest
- [x] Run end-to-end against test fixtures + live data, iterate on prompt

**Exit:** Manual digest produced, source-grounding validation passes, narratives read well.

---

## Phase 1 — Automation + Web UI ✅ DONE (2026-06-28)
**Goal:** Pipeline runs unattended; web UI loads.

- [x] Move herald CLI to optiplex as systemd services
- [x] `herald-collect.timer`: every 30min during UTC 12–20 (= AEST 22–06 = US EDT peak); every 2h off-peak
- [x] `herald-digest-morning.timer` (20:00 UTC = 06:00 AEST, covers last 16h) + `herald-digest-afternoon.timer` (04:00 UTC = 14:00 AEST, covers last 8h)
- [x] Flask + gunicorn on 127.0.0.1:8060, reverse-proxied by nginx
- [x] Bootstrap-free dark theme matching jaaaames.com aesthetic (`#0d1117` bg, `#34d399` accent, monospace)
- [x] Cert expanded to include `herald.jaaaames.com` SAN via `certbot --expand`
- [x] Debug log wired to `/var/log/herald/*` + `pipeline_runs` table; `/debug` route

**Exit:** Digests arrive twice daily. Web UI loads. Debug log shows green.

---

## Phase 2 — Synthesis sophistication 🟡 PARTIAL
**Goal:** Better cluster quality, story continuity, per-story-type prompts.

- [x] **Embedding-based pre-filter** (Phase 2–3 crossover; shipped 2026-06-28). `text-embedding-3-small`. Threshold tuned to 0.50 against real X data.
- [x] **Two-stage timeline-aware synthesis** (shipped 2026-06-29). Stage 1 sees all candidates, produces `timeline_map` + `zeitgeist` + `debate_lines` + `signal_vs_noise`. Stage 2: one LLM call per story, sees only its theme's posts. Editorial reasoning across the timeline, not per-cluster regurgitation.
- [ ] **Story continuity** — look back at recent digests to recognise ongoing stories; populate `continuity_story_id` + `what_changed`. Cost trade-off: prompt grows by ~previous digest summary. Defer until prompts are otherwise stable.
- [ ] **Ranker tuning** — refine `log(follower_count + 10)` normalisation against real engagement data. Default is OK, may need adjustment.
- [ ] **Per-story-type prompts** — news event vs. discussion vs. announcement vs. personal observation. Defer until continuity ships.

**Exit:** Continuity works without bloating LLM input costs. Smaller accounts surface proportionally without feedback. Per-story prompt quality is measurably better than single-prompt for all categories.

---

## Phase 3 — Parser v2: parent tweets & thread expansion ⏭ NEXT
**Goal:** Herald sees replies in full context, not just the reply text.

The current `parser.py` emits a stub parent_tweet (id + url, `text=null`)
for replies, because bird's `--json-full` does NOT inline the parent
tweet's content. Herald's pipeline never follows the parent. Result: when
a post is a reply to something substantive, the digest narrative either
quotes the reply without context, or skips it entirely.

This phase fetches full parent context so the LLM can summarise reply
chains in their actual conversational context.

**Scope:**
- When a candidate cluster contains reply-type posts, fetch the parent
  tweet(s) via `bird read <parent_id> --json-full` and resolve the chain
  to depth N (configurable, default 1 or 2).
- When a post is part of a thread (consecutive posts by same author),
  fetch the thread root + most recent N for context.
- Store fetched context either in the post's `raw` field or in a side
  table (`post_context`); mark in `posts.context_fetched_at`.
- Surface in the LLM prompt as **context posts** — distinct from
  candidate posts, used to ground reply understanding but not themselves
  cited unless they make the cut.

**Exit criteria:**
- Reply-type clusters include parent context in the LLM input
- Thread chains resolved end-to-end (or to configured depth) for in-feed posts
- Parser stays pure-data; bird read calls happen in `herald_digest.py` /
  `herald_synthesise.py`, not in `parser.py` (preserves parser.py's
  input-from-stdin / output-to-stdout contract)
- No regression in heuristic clustering when context fetch fails
  (graceful degradation: story falls back to reply-only narrative)
- Cost is bounded (default max 50 context fetches per digest)

**Open questions:**
- Fetch eagerly during `collect` (every cycle) or on-demand during digest
  synthesis?
- Depth limit per chain (1, 2, configurable via `HERALD_PARENT_DEPTH`)?
- How does fetched context interact with the embedding pre-filter (parent
  tweet may or may not be its own cluster)?
- Thread detection: consecutive posts by same author within a time window,
  or Twitter's own thread markers?
- Do we cache bird read responses in `herald_cache` to avoid re-fetching
  the same parent across digests?

---

## Phase 4 — Multi-source ⏭ PLANNED
**Goal:** Herald absorbs a second source cleanly, demonstrating the
source-agnostic architecture.

X is commentary. Most accounts James follows are not journalists; they're
people commenting on news. Herald's Phase 4 adds a **factual baseline
layer** (likely RSS first, since the original news-aggregation concept
was Miniflux/RSS) so the digest becomes "X is talking about Y; here's
what Y actually is."

- [ ] Add RSS collector (`herald_collect_rss.py`). Drop posts into the same
  `posts` table with `source='rss'`.
- [ ] Cross-source clustering: an X post and a Reuters article about the
  same event become members of the same story.
- [ ] Two-layer digest: news anchor + X commentary, with source provenance
  in the UI.
- [ ] Validate that the source-agnostic schema actually works without
  touching clusterer/ranker/summarizer (the whole point of designing it
  source-agnostic from the start).

**Exit:** Architecture absorbs a second source cleanly. Herald becomes the
unified "my information diet" service.

---

## Phase 5 — Accumulation-based publishing ⏭ DEFERRED
**Goal:** Herald publishes when it has enough good content, not on a clock.

Currently Herald publishes a digest at fixed times (06:00 and 14:00 AEST).
Time-based scheduling is a reasonable default, but doesn't reflect the
actual content flow: some 8-hour windows have one big story worth
surfacing; others have four or five.

This phase shifts Herald to **accumulation-based publishing** with a
configurable threshold and a publication-mode toggle.

**Scope:**
- After ingest + embed (the Phase 0–1 pipeline), continuously assess the
  candidate pool for "story readiness"
- A digest publishes when the readiness score crosses a threshold:
  - **Low threshold (e.g., 1)** → blog-style stream of independent
    articles as they become ready
  - **Mid threshold (e.g., 3–5)** → digest-like publication
  - **High threshold (e.g., 5–10)** → batch publication
  - **Configurable** via env: `HERALD_PUBLISH_MODE=stream|digest|batch`,
    `HERALD_MIN_STORIES=N`, `HERALD_MAX_WAIT_HOURS=N`
- The scheduler becomes an evaluator, not a timer; the "twice daily at
  06:00/14:00" digest is one specific configuration among many
- Time-based mode remains available as the default — no breaking change

**Why this is deferred:**
- The quality of the readiness assessment depends on having multiple
  sources. With only X (commentary), "is this worth publishing?" is
  harder to evaluate than "is this worth framing against a news anchor?"
- Phase 4's news-anchor layer provides the factual baseline that makes
  readiness scoring meaningful

**Open questions:**
- Readiness scoring formula: story count × quality × freshness? Or
  something LLM-assisted (cheap call)?
- How does accumulation mode interact with continuity (ongoing stories
  should publish even if low count)?
- Time budget: max 15-min read, but if accumulation mode produces 20
  stories, do we cap, expand, or split into multiple publications?
- Backwards compat: keep time-based mode as the default; explicit
  config switches to accumulation
- Should "stream" mode publish to a different surface (RSS feed? new
  `herald/stream` page?) or just keep appending to the main digest page?

**Exit:** Herald can run in stream, digest, or batch mode; mode
switchable via env without code changes. Readiness scoring is robust
enough that no newsworthy content gets held back.

---

## Phase 6 — Deep dives / context gathering ⏭ STRETCH
**Goal:** Technical or low-context stories have richer grounding in the digest.

Originally a dream of the prior iterations, lost when the agent was
removed from the synthesis path. Phase 6 may add a focused-research
sub-pipeline that produces context blocks the synthesizer can include
as grounded background.

Possible implementations (explore during Phase 4):
- OpenRouter tool-use to fetch 1–3 related articles (Wikipedia, source's
  own article links) for context
- A separate "research" LLM call before the synthesis call, returning a
  context block
- Web search via a separate API (Brave, Tavily) for fact background

**Cost trade-off:** A research call roughly doubles the per-story LLM
spend. Use selectively — only for stories flagged as low-context or
technical.

**Exit:** Technical or low-context stories have richer grounding in the
digest. The dream becomes real without overloading anything.

---

## Recently completed (status reference)

For finer detail on what shipped, see CHANGELOG.md. Quick reference:

- 2026-06-29: Two-stage timeline-aware synthesis (gpt-oss-120b, reasoning model)
- 2026-06-28: Embedding pre-filter (cosine 0.50, 40 candidates)
- 2026-06-28: First automated digest, web UI live, scheduler live
- 2026-06-27: Parser fixes (media URLs, parent_tweet stub, note_tweet)
- 2026-06-27: Schema with source-agnostic `source` field, accounts + posts tables
- 2026-06-27: Manual digest CLI working end-to-end against test fixtures
---

## Backlog (not committed to any phase)

Items that have come up but don't belong to a planned phase. Capture here
so they don't get lost; promote to a phase when the time comes.

### Admin panel / admin login ⏭ BACKLOG (raised 2026-07-04)

Herald has accumulated enough env-driven knobs (`HERALD_QUOTE_DEPTH`,
`HERALD_CONTEXT_MAX_FETCHES`, `HERALD_MODEL`, `HERALD_PUBLISH_MODE`,
`HERALD_MIN_STORIES`, `HERALD_DRY_RUN`, fetch timeouts, the synthesis
temperature, the embedding threshold, ...) that surface area for them
through the web UI is starting to make sense. Right now every config
change requires editing `/etc/systemd/system/herald-*.service` or the
collect.env file and restarting timers.

Scope ideas (for when we get to it):
- Read-only `/admin` route: show current values of all env-driven knobs,
  recent pipeline_runs, the quote/thread cache stats (already in
  `get_context_stats`), and the last 24h cost from OpenRouter.
- Auth: shared bearer token in a cookie / `Authorization` header, set via
  a separate env var. Single-user deployment; no real user model needed.
- Edit-and-apply: writes the new value to the relevant service's
  `EnvironmentFile` and `systemctl restart`. Brief downtime per change,
  acceptable for single-user infra.
- Live tail of `/var/log/herald/{collect,digest}.log` over a WebSocket,
  for the "is it working right now?" check.

Trigger: when we hit our third "I want to tune X without ssh+restart"
moment. Currently we've had two (the synthesis temperature, the
embedding threshold). One more and it's worth doing.

