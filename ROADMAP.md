# Herald — Roadmap

Phases are ordered by current plan. Phase 0/1/2/3/3.5/3.5.1 are done.

| Phase | Status | What |
|---|---|---|
| 0 — Foundation | ✅ DONE | bird + parser + manual digest |
| 1 — Automation + Web UI | ✅ DONE | systemd timers, gunicorn, nginx, cert |
| 2 — Synthesis sophistication | 🟡 PARTIAL | embeddings ✅, two-stage ✅, continuity pending, ranker tuning pending, per-story prompts pending |
| 3 — Parser v2 | ✅ DONE (2026-07-04) | parent tweets of replies + thread expansion + recursive quote chains |
| 3.5 — Inline-quote fix | ✅ DONE (2026-07-04) | seed quote_context at ingest; chain walker to HERALD_QUOTE_DEPTH (default 3) |
| 3.5.1 — Quote+reply unravel | ✅ DONE (2026-07-04) | walk both quote AND reply dimensions; reply-parent fetch per quote target; HERALD_REPLY_PARENT_DEPTH=2, HERALD_FOLLOW_REPLY_PARENTS toggle |
| 3.6.1 — Image context | ✅ DONE (2026-07-05) | vision LLM describes attached photos, cached in image_context, IMAGE CONTEXT block in stage 2 |
| 3.6.2 — Link context | ✅ DONE (2026-07-06) | HTTP GET + stdlib HTMLParser extracts article body, cached in link_context, LINK CONTEXT block in stage 2. No LLM in the middle. |
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

## Phase 3.5.1 — Quote+reply unravel ✅ DONE (2026-07-04)

**Goal:** Unravel chains that interleave quote and reply dimensions
(e.g. "Big if true" → reply → quote of reply → reply → quote → quote → source).
Phase 3.5 followed only the quote dimension; reply-targeting quote chains
were truncated at the first reply boundary.

**What shipped:**

- `parser.parse_quoted` now propagates `conversation` metadata for the
  quoted tweet (id, parent_id, is_reply). Without this, the walker
  couldn't tell the quoted tweet was a reply.
- `herald_context._expand_context_chain` (renamed from `_expand_quote_chain`)
  walks both quote and reply dimensions orthogonally. At each quote target
  reached, if it's itself a reply, also fetch the parent and recurse on
  ITS quote chain. Quote depth and reply depth decrement independently.
- `attach_context_to_candidates` emits `_quote_context` as a list of
  `{"tweet": ..., "_chain_kind": "quote"|"reply"}` items. Synthesise prompt
  renders quote levels with `↳` and reply-parent edges with `⤴`.
- `herald_context.estimate_context_cost` — new helper for the future
  admin panel: counts fetches and approximate context tokens for a batch.
- `HERALD_FOLLOW_REPLY_PARENTS` env toggle (default True) — master kill
  switch for reply-chain walking. Set to 0 to revert to Phase 3.5
  behaviour (quote-only).

**Verified on James's tweet 2073288532110557675** (the "Big if true" / Hamas
chain — 7 tweets spanning 4 quote hops + 2 reply hops):

```
Before 3.5.1: 1 context item (just @KelseyTuoc's reply — chain invisible)
After  3.5.1: 6 context items with full quote+reply structure:
  • @KelseyTuoc (quote-tweet of Andrew)
  ⤴ @Andrew_Semenza (Kelsey's reply parent)
  ↳ @KelseyTuoc (Andrew's quote target, also a reply)
  ⤴ @davidhazony (Kelsey's other reply parent)
  ↳ @OsherFeldman (davidhazony's quote target)
  ↳ @strangerous10 (OsherFeldman's quote target — THE SOURCE)
```

**Cost:** 5 fetches per chain worst case. With HERALD_CONTEXT_MAX_FETCHES=30
shared budget, ~4 such chains per collect before trim.

**Tunables:**
- `HERALD_QUOTE_DEPTH` (default 5, was 3)
- `HERALD_REPLY_PARENT_DEPTH` (default 2)
- `HERALD_FOLLOW_REPLY_PARENTS` (default 1)

**Tests:** 5 new cases (13–17). All pass.

**Additional tests added 2026-07-05** (18–20) — robustness coverage:
- 18: pure reply chain (A→B→C→D, no quotes) — walker follows reply edges to depth cap
- 19: cycle protection (A↔B) — visited set breaks the back-edge
- 20: 8-level deep quote chain with `HERALD_QUOTE_DEPTH=5` — walker fetches 4 (levels 2–5), stops, leaves 6–8 uncached

20/20 green.

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


---

## Phase 3.6 — Image + link context expansion ✅ DONE

**Goal:** Herald synthesises with awareness of the actual content of
attached media and outbound links, not just the post text. Both
sub-phases shipped.

### Phase 3.6.1 — Image context ✅ DONE (2026-07-05)

Vision LLM describes attached photos via Gemini 2.5 Flash; cached in
`image_context`; rendered as `🖼` IMAGE CONTEXT block in stage 2. 8
tests added. Default model: `google/gemini-2.5-flash` (was
`openai/gpt-4o-mini`). Default per-story cap: 1. Videos and GIFs
skipped at extract time.

**Open decision (carried forward):** Stage 2 default model is still
`openai/gpt-oss-120b` (cheaper for testing). Switching to a vision-
capable model would let the LLM actually use the IMAGE CONTEXT block
— but the May/June 2026 test runs suggest the vision descriptions
are often thin. Revisit only if descriptions themselves are the
problem (not the model).

### Phase 3.6.2 — Link context ✅ DONE (2026-07-06)

HTTP GET + stdlib HTMLParser extracts article body, cached in
`link_context` (PK = canonical URL, tracking-stripped). Rendered as
`📰` LINK CONTEXT block in stage 2, between IMAGE CONTEXT and the
"Write the story JSON now." footer. No LLM in the middle — per James
"AI summary of an AI summary" loses grounding.

**Extraction strategy:** scope text to inside a landmark container
(`<main>`, `<article>`, `role="main"`, or a div with an article-body
class like `RichTextStoryBody` / `article-body` / `post-content`).
Skip-class chrome (carousels, share buttons, ledes, sidebars, author
bios) excluded even when nested inside `<main>`. 16 tests including a
real-world regression test against `/tmp/apnews_full.html`.

**Verified on digest 21 story 1:** the AP News article body added
facts (referee name Raphael Claus, 1962 historical precedent, direct
quotes from Trump and Belgian coach Rudi Garcia, Court of Arbitration
for Sport legal angle) that were in NONE of the 3 source X posts.
Cost delta per stage-2 call: +$0.000144 (still trivial at scale).

**Default knobs:** `HERALD_LINKS_PER_STORY=2`,
`HERALD_MAX_LINK_FETCHES_PER_CYCLE=10`, `HERALD_LINK_TIMEOUT=10s`.
9 new `HERALD_LINK_*` env vars; all whitelisted in `herald_env.LOAD_KEYS`.

**Known limitation:** `SKIP_CLASS_PATTERNS` is a hand-curated list.
Works for AP News out of the box; other sites (Guardian, NYT,
Substack, Reuters) will likely need more patterns added as we hit
them. Framework is general; specific patterns are not.

---

## Phase 3.7 — Output-quality tweaks ⏭ IN PROGRESS (added 2026-07-08)

The digest replaces the time the reader would have spent scrolling X.
Two weeks in, it works for "what happened" coverage but the editorial
voice is too uniform — too "news / signal / debate / consensus," too
little room for the light, off-beat, one-shot tweet that hits
different in its native habitat. The ranker favours clusters of ≥4
posts and high-follower accounts; the stage-2 prompt doesn't
explicitly allow single-post stories. Goal: keep the source-grounding
invariant, but widen the editorial range.

**Approach:** ship as five discrete passes so each can be tested in a
dry-run before moving on. No knob is taken from default until the
previous pass is verified to produce better digests without
regressing coverage or cost.

### Phase 3.7.1 — Prompt softening ✅ DONE (2026-07-09)

Scope refined vs the original plan: kept `zeitgeist` (the day-overview
list still has the most UI value), dropped `debate_lines` and
`signal_vs_noise` from both prompt and storage. Anchored the voice to
concrete reference points (Daring Fireball / The Verge / Rest of World)
rather than abstract "casual chat." Stage-2 system prompt now includes
a `VOICE & SCOPE` paragraph with explicit allowances for single-post
stories, off-beat / weird / sideways moments, and matching the
source's humour register. Source-grounding rules remain HARD —
"when in tension, grounding wins." `--top` default raised 5 → 7.

**Files changed:** `herald_synthesise.py` (STAGE2_SYSTEM, STAGE1_SYSTEM,
stage1 stub), `herald_digest.py` (theme-to-posts, storage call, --top
default, print guard from earlier hotfix), `herald_db.py`
(upsert_digest_with_stories signature + INSERT), `templates/digest.html`
(debate + signal-vs-noise blocks removed), `README.md`, `CLAUDE.md`,
`DESIGN.md`, systemd units.

**Verification:** all `herald_*` modules import cleanly; `ast.parse`
on all edited files clean; `test_image_context.py` 8/8 pass;
`test_link_context.py` 16/16 pass; dry-run digest on the live DB
runs end-to-end; web template renders cleanly with zero `debate-side`
/ `svn-grid` classes in the output.

**Next verification:** tomorrow's morning digest. Compare against
digest 25 / 26. Goal: ≥1 of 7 stories is light / off-beat / single-post
(singleton count should rise vs the recent baseline of mostly 2-3
post stories). Regressions to watch: drop in citation density (avg
`[post:ID]` per story), jump in `_parse_warning` rate, drop in any
of the harder news stories (e.g. digest 23 had 5 stories on substantive
events; we shouldn't lose all of those just to add lighter ones).
(avg `[post:ID]` per story) or a jump in `_parse_warning` rate.

### Phase 3.7.2 — Ranker flattening ⏭ DEFERRED (superseded by 3.8)
Two changes to `herald_embed.select_candidates` ranker:
- Replace `log10(followers+10)` with `log10(followers+100)` — softens
  the penalty on quirky-but-small accounts without eliminating it.
- Replace `(1 + log2(size))` with `(1 + log2(max(2, size)) * 0.3)` —
  clusters of ≥4 still preferred, but singletons no longer discriminated
  against (multiplier 1.0 instead of ~2.6).
**Verify:** same as 3.7.1 — measure singleton share in the digest.
Target: singleton stories appear ≥1× per digest without dropping
the high-engagement clusters.

### Phase 3.7.3 — Age-adjusted engagement score + flat 2h cadence ✅ DONE (2026-07-10)

The ranker systematically over-ranked older tweets. A 24h-old tweet with
500 likes (21/h) beat a 1h-old rising tweet with 50 likes (50/h) just
because raw engagement was bigger. The interesting moment in a tweet's
life is the rise, not the plateau; the ranker was rewarding plateau.

**Fix:** divide engagement by hours-since-posted in `cluster_score`.
`age_hours = max(0.5, (now - created_at) / 3600)`. Cluster score
becomes `size_boost × mean_age_adjusted_engagement × follower_weight`.
Single snapshot, no refresh, no schema change.

**Cadence:** dropped collect from peak 30min / off-peak 2h to a flat
2h. The over-fetch (`-n 200` per run) already produces most of the
timeline; the age-adjusted ranker is robust to missing the most
recent half-hour. Half the bird calls per day, same editorial outcome.

**What we explicitly did NOT do:** periodic metrics refresh. Reasoning:
a 2h-old tweet that has had time to accumulate *some* engagement is
already informative under the new score. The trajectory signal is
nice-to-have, not need-to-have. If variety or quality still suffers
after 3.7.3 + 3.8 land, revisit as a Phase 4 stretch item. The
schema migration cost (save metrics + timestamp, compute Δ) is real
and not worth it for the marginal accuracy gain.

**Files changed:** `herald_embed.py` (cluster_score now age-adjusted,
takes `now_ts` for determinism), `test_age_adjusted_ranker.py` (new,
5 tests including a regression test that the new formula ranks
rising tweets above stale ones).

**Cadence change (systemd):** edit `/etc/systemd/system/herald-collect.timer`,
replace the two `OnCalendar` lines with a single `OnCalendar=*:00/2` line
(every 2 hours starting at 00:00 UTC). User to apply manually (root).
Service file unchanged.

**Verification (digest 35, 2026-07-10):** test suite passes 5/5.
The regression test (Test 5) explicitly demonstrates: with the old
formula, the stale tweet scored 501.0 vs the fresh tweet's 51.0 (10×
bias). With the new formula, the fresh tweet scores 153.2 vs the stale
65.6 (a 2.3× *reversal* of the bias). The new ranking is what we want.

### Phase 3.7.4 — Stage 1 editorial restructure ⏭ SUPERSEDED by 3.8.1
Restructure `stage1_timeline_map` output from
`{themes, debate_lines, signal_vs_noise, timeline_map}` to a simpler
`{themes: [{label, angle, post_ids, salience}], editorial_notes}`.
Less structure → more editorial latitude in stage 2. Stage 2 reads
the theme list + one-line pitches, picks what to write.
**Verify:** stage 1 returns valid JSON more reliably (current
`debate_lines / signal_vs_noise` structure occasionally trips the
model into nested-array edge cases); digest output quality unchanged
or improved.

### Phase 3.7.5 — Story-type prompt variants ⏭ SUPERSEDED by 3.8.2
Split `stage2_story` into `news_story()`, `vibe_story()`,
`one_post_story()`. Different prompts: news = full treatment with
context + quotes; vibe = single-paragraph mood capture; one_post =
"this tweet landed" framing. Stage 1's `editorial_notes` field
suggests the type per theme.
**Verify:** dry-run digest has visibly different treatment for news
vs vibe vs one_post stories. No regression in citation density for
news stories.

### Out of scope for 3.7 (deferred)

- **Model swap to a prose-strong LLM.** Stay on `gpt-oss-120b` until
  3.7.1–3.7.5 are exhausted. The model is fine for text; over-thinking
  on reasoning tokens may be the issue, not capability.
- **Multi-source (Phase 4).** Bigger product change. The Phase 3.7
  tweaks operate on X-only corpus; Phase 4 changes the corpus itself.

### Backlog raised during planning

- **Hotfix 2026-07-08** (digest 24, story 91): stage 2 returned valid
  JSON with empty headline/story; CLI crashed at print time. Fixed in
  `herald_synthesise.py:stage2_story` (validate non-empty fields, fall
  back to theme-derived headline + stub body with `_parse_warning`)
  and `herald_digest.py:504` (`s.get('headline') or '<no headline>'`).
  No more silent empty story slots; CLI doesn't crash on partial
  success.
- **Stage 2 misassignment observed:** digest 24 story 91 grouped an
  unrelated FDNY post with a World Cup tweet. Stage-1 cluster is
  suspect. Worth a sweep when 3.7.4 lands (less structured output may
  make it harder for stage 2 to misassign, easier to spot when it
  does).

### Backlog — code health

Captured 2026-07-09 from a code health audit (see CHANGELOG 2026-07-09
and 2026-07-10). Originally framed as "PR 2 refactors, deferred";
MR-1, MR-4, MR-5, and MR-6 have since shipped (2026-07-10).
Remaining: schema cleanup and the `/debug` observability pass.

- **MR-1 — `generic_cache_upsert` in `herald_db.py`.** ✅ DONE
  (2026-07-10). Shipped as a smaller helper than originally planned:
  `_protected_set_clause(table, col)` returns the
  `CASE WHEN excluded.fetch_status = 'success' THEN excluded.X ELSE
  table.X END` SQL fragment. Used by all four cache upsert functions
  (image, link, conversation, quote). Net ~−20 lines, identical SQL
  emitted, each function still obviously readable.
- **MR-4 — Split `cmd_digest` (herald_digest.py:198–526, 328 lines).**
  🟡 PARTIAL (2026-07-10). Shipped the highest-leverage piece:
  `_attach_all_contexts(candidates)` replaces the 3 near-identical
  try/except blocks (~50 lines → 1 call). cmd_digest is now ~280
  lines. The remaining work (split stage-1 / stage-2 / persist into
  named helpers) was deferred — would be a bigger refactor that
  doesn't reduce surface area as much.
- **MR-5 — Move debug scripts to `scripts/`.** ✅ DONE
  (2026-07-10, pre-commit cleanup). `digest_readable.py` and
  `trace_digest.py` moved to `scripts/` with a `scripts/README.md`.
  Other one-offs (`scratch_*.py`, `check_inline_quotes.py`,
  `validate.py`) deleted outright.
- **MR-6 — `parser.py:108` `needs_context_fetch: True` flag is dead.**
  ✅ DONE (2026-07-10). Dropped (chose drop over wire; `post_type ==
  "reply"` is the cleaner signal that `enrich_batch` already uses).
- **Schema cleanup — drop confirmed-dead columns in one migration.**
  ⏭ DEFERRED. `posts.raw` (SW-3, never populated, ~75 × 10 KB saved
  per collect). `accounts.verified` / `accounts.bio` /
  `accounts.display_name` / `accounts.notes` (SW-4/5, parser doesn't
  extract). `stories.section` (SW-6, only `'story'` ever written).
  `stories.continuity_story_id` / `stories.what_changed` (SW-7, never
  written). `digests.duration_estimate_minutes` (SW-8, always NULL).
  Real migration with `ALTER TABLE` + rewrite. Defer to when those
  tables next need schema work.
- **Wire `get_*_context_stats` helpers into `/debug`** (SW-1).
  ⏭ DEFERRED. Currently unused; would surface image / link /
  conversation cache hit rates. UX change — defer to a dedicated
  observability pass.

---

## Phase 3.8 — Items tier + sliding-scale stories ✅ DONE (2026-07-10)

The variety problem. The digest reliably returns 3-5 stories per cycle,
all on the same heavy themes (Bitcoin, AI, US politics). Off-beat,
high-engagement singletons never surface as stories — they don't form
multi-post clusters, so stage 1 drops them. The reader gets a digest
that's well-written but monotonous.

The previous version of Herald (2026-02-26 archive:
`https://jaaaames.com/digest/archive/2026-02-26-pm.html`) solved this
with a three-tier presentation: 1 lead story + 6 features + ~19
timeline-pulse one-liners. About 26 unique picks per digest, with
deliberate variety. The current pipeline collapses all of that into
"1-3 paragraph narrative per theme" — the structural difference is
the one-liner tier, not better selection.

### Diagnostic evidence (digest 30, 2026-07-09 morning)

40 candidates, 5 zeitgeist themes, only 3 made it to `stories_to_write`.
20 of the 40 candidates (50%) were not assigned to any zeitgeist theme.
Notable orphans (would have been excellent timeline-pulse picks):

- **Bonnie Tyler dies aged 75** (AP + BBCBreaking, eng=8564) — major news
- **Christian Pulisic fractured leg at World Cup** (AP, eng=2752) — major news
- **Count Binface vs Nigel Farage** (AP, eng=2968) — UK political humor
- **NEO's 25 Degrees of Freedom hands** (BerntBornich, eng=19233) —
  highest engagement of the whole set; robotics breakthrough
- **Musk "@finkd @Scobleizer Jinx"** (eng=10163) — pure X-native humor
- **Dan Neidle on being interviewed by Gary Stevenson** (eng=6788) —
  long-form personal post
- **Canada admits Trudeau climate agenda was a scam** (eng=5189) — news
- **Cernovich on Anthropic / "left wing NGO's"** (eng=8876) — political
- **WebX 2026 / Takaichi** (gerovich) — Japan / crypto event

These are exactly the kinds of items the old version surfaced as
"timeline pulse." Today's pipeline throws them away.

### Approach: the editor decides, the writers follow

The instinct was to add a third pipeline stage. That was wrong. The
candidates already contain everything we need; the issue is that
stage 1 currently treats itself as a *formatter* (group into themes,
output N stories) rather than an *editor* (decide what's worth
covering today, at what depth, in what shape).

The fix is to give stage 1 the editor's full discretion over scope.
No item quota, no rigid story/item split, no expectation that a
digest must contain a certain count. If today genuinely has only
three angles, three is the right answer. If today has twenty, twenty
is the right answer. The model gets to decide.

Two coordinated changes:

1. **Stage 1 is the editor.** Its output gains two new fields beyond
   `stories_to_write`:
   - `items`: optional array of `{post_id, why, kind}` for individual
     posts the editor wants surfaced but doesn't think deserve a
     paragraph each. `kind` is the editor's own tag — `'news'`,
     `'humor'`, `'quote'`, `'meme'`, `'off-beat'`, `'link'`,
     `'sidebar'`, whatever fits. Use it to express what the item is
     for, not to constrain.
   - `editor_notes`: a freeform 1-2 sentence paragraph from the editor
     to the writers (stage 2) about today's tone, what to lean into,
     what to skip. Pure editorial direction.
   The editor decides what's a story, what's an item, and what to
   leave out entirely. The prompt tells it what's *possible* (themes
   for full treatment; items for single posts; leave gaps where the
   timeline doesn't have anything) — not what's *required*.
2. **Stage 2 follows orders.** When given a theme, it writes a story
   at the depth the theme deserves. When given an item, it writes a
   short caption or passes the tweet through. The editorial direction
   in `editor_notes` is passed through and used to set tone.

A useful mental model: stage 1 is a magazine editor assigning
coverage to writers. Sometimes the editor assigns six feature pieces
and a sidebar. Sometimes the editor assigns one long lead and a
handful of one-liners. The editor has the whole timeline in mind and
knows what the issue needs.

Storage: existing `stories` table handles both tiers. Stories are
rows with the existing schema. Items are rows with `coverage_type='item'`
(extending the existing `'one_shot' | 'ongoing'` enum — same row
shape, just shorter content). The web UI renders both — items above
the full stories with lighter visual treatment.

### Out of scope for 3.8

- **A new pipeline stage.** Not needed. Candidates already include
  what we need.
- **Raising `HERALD_MAX_CANDIDATES`.** 40 is fine; the candidates are
  good. The issue is downstream.
- **The original Phase 3.7.5 (story-type prompt variants).** Sliding
  scale inside one prompt replaces it. Cleaner.
- **A separate "items LLM call".** Not needed. Stage 1 already sees
  the candidates; asking it to label items in the same response is
  essentially free.
- **A quota on items or stories.** The editor decides. If today
  genuinely has only three angles, three is the right answer. If it
  has twenty, twenty is the right answer. The previous version's
  ~26 unique picks was a function of drawing from a much larger
  corpus with no shortlisting — not a target to hit.

### Sub-phases

#### Phase 3.8.1 — Stage 1 becomes the editor ⏭ FIRST

Extend `STAGE1_SYSTEM` in `herald_synthesise.py` (currently ~lines
224-256). Reframe stage 1 as an editor with discretion, not a
formatter. The prompt changes are mostly about *voice and permission*:

- Replace the current "output these fields" framing with "you are
  the editor of today's digest. Decide what's worth covering and at
  what depth."
- Add to the JSON output schema:
  ```json
  items: optional array of {post_id, why, kind}. post_id is a cited
    candidate ID. why is 1 sentence on why the editor chose it.
    kind is the editor's own tag — 'news', 'humor', 'quote', 'meme',
    'off-beat', 'link', 'sidebar', whatever fits.
  editor_notes: optional 1-2 sentence paragraph from the editor to
    the writers about today's tone, what to lean into, what to skip.
  ```
- Tell the editor explicitly: "If today only has three angles, write
  three stories. If today has twenty distinct things, surface them
  all — but pick the right shape for each (story vs item vs skip
  entirely). Don't pad; don't artificially broaden."
- Items should be the candidates that didn't form a theme —
  singletons, short replies, off-beat observations, anything that
  would otherwise be silently dropped. Salience guidance: "include
  if engagement is high OR if it's the kind of thing a friend would
  want to flag."

Update the dry-run stub at ~line 268 to include `items: []` and
`editor_notes: ""`.

Verify: dry-run digest on the live DB. Inspect the model's first-pass
items list. Eyeball whether it has the discretion it needs, or whether
the prompt is still pushing it toward a fixed output shape.

#### Phase 3.8.2 — Stage 2 sliding-scale prompt ⏭ PLANNED

Update `stage2_story` to take a `n_sources` parameter (count of posts
in the theme cluster) AND pass through `editor_notes` from stage 1.
The prompt needs to know both: how much source material it has, and
what tone the editor wants.

Append to `STAGE2_SYSTEM` (or to the user message):

```
SCOPE: this theme has {n_sources} source posts.
- n_sources >= 5: full feature treatment, 3-5 paragraphs with embedded
  quotes and inline [post:ID] citations.
- n_sources 2-4: brief, 1-2 paragraphs.
- n_sources == 1: 1-2 sentences framing the tweet, or pass it through
  verbatim with [post:ID] citation if the tweet is already complete.

EDITOR'S NOTES (from the editor): {editor_notes}
```

Existing `coverage_type='one_shot' | 'ongoing'` enum stays. Items
generated by this sliding scale get `coverage_type='item'`. The
output JSON schema (headline, story, topic, key_quotes) stays the same.

Verify: dry-run digest. Confirm that single-post themes produce a
1-line item, 2-4 post themes produce a brief, 5+ post themes produce a
full feature.

#### Phase 3.8.3 — Persist items ⏭ PLANNED

Extend `herald_db.upsert_digest_with_stories` to accept an `items` list
and write them as `stories` rows with `coverage_type='item'`. No
schema migration — `coverage_type` already exists and accepts any
string. The `story` column has a length cap of ~280 chars for items
(enforced in the prompt or post-processor).

Verify: run live digest. Confirm items rows appear with `coverage_type='item'`
and short `story` content.

#### Phase 3.8.4 — Web UI: render items tier ⏭ PLANNED

Update `templates/digest.html` to render items above the full stories,
with lighter visual treatment (smaller font, no border, no angle line).
Each item shows: tweet text (or post link) + 1-line gloss + author
handle + citation.

Verify: live digest URL. Eyeball the variety — should look like the
2026-02-26 archive's "Timeline Pulse" section.

### Updated phase ordering (post-3.8)

The original Phase 3.7 sub-phase ordering (3.7.2 next, then 3.7.3, 3.7.4, 3.7.5)
is now reprioritised:

- **3.7.2 — Ranker flattening.** ⏭ PLANNED → ⏭ DEFERRED. The candidates
  are good as-is; 3.8 surfaces what the ranker would have otherwise
  filtered. Revisit if 3.8 doesn't fix the variety problem.
- **3.7.3 — Age-adjusted engagement + flat 2h cadence.** REWRITTEN
  twice. The original "raise bird volume" idea was wrong; the
  metrics-refresh plan was right diagnosis but wrong fix. Final
  shape: divide engagement by hours-since-posted in the ranker; no
  new fetcher, no schema change. Plus drop collect cadence to a
  flat 2h. ✅ DONE (2026-07-10).
- **3.7.4 — Stage 1 editorial restructure.** Now redundant with 3.8.1
  (3.8.1 does the prompt extension cleanly). ⏭ PLANNED → ⏭ SUPERSEDED.
- **3.7.5 — Story-type prompt variants.** Superseded by 3.8.2 (sliding
  scale in one prompt is cleaner than 3 variants). ⏭ PLANNED → ⏭ SUPERSEDED.

Net effect: 3.7 closes out with 3.7.1 and 3.7.3 done, the other
three sub-phases deferred or superseded. 3.8 becomes the active next
phase.

### Verify (overall, after all four 3.8 sub-phases)

Dry-run + live digest. Compare against digest 30 (3 stories, 0 items)
and digest 28 (5 stories, 0 items). Goal: ~3-5 stories + ~8-12 items
per digest. Compare variety against the 2026-02-26 archive structure
(1 lead + 6 features + 19 pulse) — we don't need to match the count,
we need to match the *kind* of variety.

Cost: one extra JSON field per stage-1 call (free), one extra stage-2
field per theme (free), one extra storage row per item (8-15 rows ×
~280 chars each ≈ 4 KB per digest). Total cost delta: negligible.

**Verification (digest 34, 2026-07-10):** 5 stories + 5 items = 10
entries. Total cost $0.0065 (well under target). Items: humor (×2),
news, political, off-beat — real variety from the day's timeline.
Stage 1 finish `stop` (reasoning cap holds). All items render with
`.story-item` class in the web UI after `herald-web.service` restart.
