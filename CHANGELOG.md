# Herald — Changelog

Newest first. Dates in AEST (Australia/Sydney, GMT+10) unless noted.

---

## 2026-07-10 — Phase 3.7.3: age-adjusted engagement + flat 2h cadence

The ranker systematically over-ranked older tweets. A 24h-old tweet
with 500 likes (21/h) beat a 1h-old rising tweet with 50 likes (50/h)
just because raw engagement was bigger. The interesting moment in a
tweet's life is the rise, not the plateau; the ranker was rewarding
plateau.

**What's new:**

- `herald_embed.cluster_score` now divides engagement by hours since
  the post landed (`age_hours = max(0.5, (now - created_at) / 3600)`).
  Cluster formula: `size_boost × mean_age_adjusted_engagement ×
  follower_weight`. Same engagement formula
  (`likes + 2*retweets + replies`), same size boost, same follower
  weight. Just the engagement term is normalised by age.
- A new `now_ts` parameter on `cluster_score` makes scoring
  deterministic for testing.
- The metric is not passed to the LLM (stage 1 sees raw engagement).
  Age adjustment is a ranker concern, not an editorial concern —
  the editor does editorial work, not math.
- Collect cadence dropped from peak 30min / off-peak 2h to a flat 2h.
  Less infrastructure, same editorial outcome. The peak window was
  tuned for US-daytime tweet volume but the over-fetch
  (`-n 200` per run) plus the age-adjusted ranker are robust to
  missing the most recent half-hour.

**What we explicitly did NOT do:** periodic metrics refresh. A
2h-old tweet that has had time to accumulate *some* engagement is
already informative under the new score. Trajectory signal is
nice-to-have, not need-to-have. The schema migration cost (save
metrics + timestamp, compute Δ on each refresh) is real and not
worth it for the marginal accuracy gain. Defer to a Phase 4 stretch
item if it turns out to matter.

**Verification (test_age_adjusted_ranker.py, 5 tests):**

- Test 1: fresh tweet (1h, 50 likes) at 50/h scores 153.2 vs stale
  tweet (24h, 500 likes) at 21/h scores 65.6. ✓
- Test 2: 0.5h clamp prevents div-by-zero on freshly-published
  tweets. ✓
- Test 3: `now_ts` parameter works for deterministic test scoring. ✓
- Test 4: mixed-age cluster uses mean of age-adjusted values. ✓
- Test 5: regression test — old formula scores stale 501 vs fresh 51
  (10× bias). New formula reverses it: fresh 153 vs stale 66. ✓

**Cadence change (systemd, user to apply):** edit
`/etc/systemd/system/herald-collect.timer`. Replace the two
`OnCalendar` lines with a single `OnCalendar=*:00/2` line
(every 2 hours starting at 00:00 UTC), then
`sudo systemctl daemon-reload && sudo systemctl restart
herald-collect.timer`. Service file unchanged (already pulls
`-n 200` per run).

**Files changed:** `herald_embed.py` (cluster_score + new import),
`test_age_adjusted_ranker.py` (new, 5 tests),
`/etc/systemd/system/herald-collect.timer` (user to edit).

**Hard constraints unchanged:** source-grounding, JSON output schema,
image + link context, voice & scope from 3.7.1, items tier from 3.8.

**Followup:** monitor tomorrow's digests. If the age-adjusted ranker
is producing noticeably better variety (different candidates than
the pre-3.7.3 run), Phase 3.7 is closed. If not, the next thing to
try is the deferred metrics refresh — but only as a Phase 4 stretch
item, not as a 3.7.4.

---

## 2026-07-10 — Phase 3.8: editor discretion + sliding-scale stories

The variety problem. The digest reliably returned 3-5 stories per cycle,
all on the same heavy themes (Bitcoin, AI, US politics). Off-beat,
high-engagement singletons never surfaced as stories — they didn't form
multi-post clusters, so stage 1 dropped them. The reader got a digest
that was well-written but monotonous.

The fix reframes stage 1 from formatter to editor: the editor decides
what's worth covering today, at what depth, in what shape. If today
genuinely has only three angles, three is the right answer. If it has
twenty, twenty is the right answer. No quota, no rigid story/item
split.

### What's new

**3.8.1 — Stage 1 becomes the editor.** `STAGE1_SYSTEM` reframed. The
output schema gains two optional fields:

- `items`: array of `{post_id, why, kind}` for single-post picks that
  didn't form a theme. `kind` is the editor's own tag (news, humor,
  quote, meme, off-beat, link, sidebar, observation, etc.).
- `editor_notes`: 1-3 sentence paragraph from editor to writers about
  today's tone.

Both are optional. The editor can pick 0-N items and write 0-N stories.

**3.8.1 — Reasoning-token caps (hotfix).** Live digest 32 hit
`HERALD_STAGE1_MAX_TOKENS=64000` with `finish_reason=length` on the new
bigger prompt. Output was `{"final{"` plus whitespace. Cost: $0.025
for nothing. The reasoning model `gpt-oss-120b` burned the entire
budget on chain-of-thought. OpenRouter's `max_reasoning_tokens`
parameter caps it. Defaults: stage 1 = 4000, stage 2 = 2000. New env
vars `HERALD_STAGE{1,2}_MAX_REASONING_TOKENS` (whitelisted in
`herald_env.LOAD_KEYS`). Digest 33 with cap in place: cost $0.0013,
finish `stop`.

**3.8.2 — Stage 2 sliding-scale prompt.** `stage2_story` now accepts
`editor_notes`. The user message includes a `SCOPE: N source post(s)`
line plus the editor notes (when present). `STAGE2_SYSTEM` adds
guidance: `n_sources >= 5` → 3-5 paragraph feature; `2-4` → 1-2
paragraph brief; `1` → 1-2 sentence caption or verbatim pass-through.
The same stage-2 prompt handles all depths.

**3.8.3 — Items become regular stories rows.** No new enum, no
`coverage_type='item'`. Items persist as standard `stories` rows with
`theme_label = kind` (so queryable but no separate class). Storage
treats items and stories identically. Both flow through the same
`upsert_digest_with_stories` path.

**3.8.4 — Web UI renders items inline.** Same Stories section, same
row markup. Items get a `.story-item` class (lighter padding,
transparent background, accent border, no number prefix). Detected
via `post_count == 1` (covers all current item shapes; kind-list
fallback handles future multi-post items).

### Verification

**Digest 34 (2026-07-10, live):** 5 stories + 5 items = 10 entries.

- Stories: OpenAI product rollout, EU chat-control law, NEO humanoid
  hand breakthrough, Rent-control debate, AI price-war & model costs.
- Items: humor (×2), news, political, off-beat.
- Total cost: $0.0065.
- Stage 1 finish: `stop` (cap holds).
- Web UI: all items render with `.story-item` class after
  `herald-web.service` restart.

**Cost breakdown:**
- Pre-3.8 (3 stories): $0.005-0.010
- 3.8 (5 stories + 5 items): $0.0065
- Delta per digest: roughly flat. The "extra" items are each ~$0.0002-
  $0.0006 (1 LLM call with sliding-scale prompt + 1 source post).

### Files changed

- `herald_synthesise.py` — STAGE1_SYSTEM editor reframing, STAGE2_SYSTEM
  sliding scale + editor_notes, `max_reasoning_tokens` plumbing in
  `_call`, `stage2_story` accepts `editor_notes`, dry-run stubs
  updated, new env-driven constants `STAGE{1,2}_MAX_REASONING_TOKENS`.
- `herald_env.py` — whitelisted `HERALD_MAX_CANDIDATES`,
  `HERALD_MAX_ITEMS_PER_DIGEST`,
  `HERALD_STAGE{1,2}_MAX_REASONING_TOKENS`.
- `herald_digest.py` — items loop in `cmd_digest`, `editor_notes`
  pass-through, `MAX_ITEMS_PER_DIGEST` constant (default 10).
- `templates/digest.html` — `story-item` class detection (based on
  `post_count == 1` or known kind list), number prefix hidden for items.
- `static/css/herald.css` — `.story-item` lighter visual treatment.
- `ROADMAP.md` — Phase 3.7.3 rewritten (metrics refresh, not bird
  volume); 3.7.4/3.7.5 marked SUPERSEDED; 3.7.2 marked DEFERRED; 3.8
  marked DONE.

### Hard constraints unchanged

- Source-grounding rules (every claim cites `[post:ID]`, verbatim quotes
  with attribution, no fabrication).
- JSON output schema for stage 2 (`headline`, `story`, `topic`,
  `key_quotes`, `coverage_type`, `section`).
- Image + link context rendering.
- "Voice & scope" paragraph from Phase 3.7.1.

### What we're NOT changing yet

- **3.7.2 (ranker flattening)** — DEFERRED. Candidates are good as-is;
  3.8 surfaces what the ranker would have otherwise filtered. Revisit
  if 3.8 doesn't fix the variety problem in practice.
- **3.7.3 (metrics refresh)** — REWRITTEN. New plan in ROADMAP. Not
  started. Adjacent problem (better ranker input) but separate work.
- **Model swap** — stay on `gpt-oss-120b`. Sliding-scale prompt + the
  reasoning cap together give us much better cost/quality than
  swapping.
- **Multi-source (Phase 4)** — deferred. 3.8 changes output shape on
  X-only corpus; Phase 4 changes the corpus itself.

### Outstanding follow-up

- **PR 2 refactor backlog** (ROADMAP §"Backlog — code health (PR 2
  refactors, deferred)"). MR-1, MR-4, MR-5, MR-6 + schema cleanup.
  Captured 2026-07-09 from code health audit; zero behavior change.
  Ship when convenient.
- **Hotfix history**: 2026-07-08 (digest 24 empty-headline crash),
  2026-07-09 (PR1 cleanup), 2026-07-10 (digest 32 reasoning-budget
  blowout → reasoning caps).

---

## 2026-07-09 — Phase 3.7.1: editorial voice + 7 stories/digest

The digest is doing what it was designed to do (replace 30min of X scrolling
with 15min of read), but the read doesn't quite *feel* like the timeline.
Two weeks in, the voice is too uniform — too "news / signal / debate /
consensus," too little room for the off-beat, single-post, weird moment
that lands on its own. James: the read should feel like the timeline felt,
not a sanitised summary of it.

Goal of 3.7.1: keep the source-grounding invariant, but widen the editorial
range. Three changes, one tiny pass, fully reversible.

**What's new:**

- **`VOICE & SCOPE` paragraph added to `STAGE2_SYSTEM`** in
  `herald_synthesise.py`. Anchors the register to a concrete reference
  set — Daring Fireball, The Verge's feature pages, Rest of World —
  rather than "casual chat" or "news brief." Explicitly allows:
  - Single-post stories ("a single striking tweet can carry a whole
    story. Don't pad with manufactured significance").
  - Off-beat / sideways / weird / internet-native moments "on their
    own merits."
  - Matching the energy of the source posts when it's distinctive
    (funny → funny, dry → dry, absurdist → name the absurdity).
  - One meta-instruction: "humour often gets lost in synthesis — lean
    toward the source's register rather than your own; if the posts
    are joking, the story should not be solemnly explaining the joke."
  - The hard constraint is explicit: "The source-grounding rules above
    are HARD. The voice here is not — when in tension, grounding wins."

- **`debate_lines` + `signal_vs_noise` removed from the pipeline.**
  They were in the stage-1 prompt, persisted to the `digests` table
  (JSON columns), and rendered in the web UI ("lines of debate" /
  "signal vs noise" sections). None of this was consumed by stage 2
  (it reads only `stories_to_write` + per-theme `angle`), so dropping
  them carries zero synthesis risk. Changes:
  - Stage-1 prompt: blocks + dry-run stub + comment about every
    observation citing ≥1 source post updated to refer to `zeitgeist`
    only. `stories_to_write` cap raised from 3-5 to 3-7.
  - `herald_digest.py`: theme-to-post mapping no longer walks
    `debate_lines` or `signal_vs_noise.noise_patterns`. The dead
    `noise_pids` extraction loop removed.
  - `herald_db.upsert_digest_with_stories`: kwargs dropped. INSERT
    still lists the columns and passes `None` (DB schema unchanged,
    columns retained for back-compat with old digests).
  - `templates/digest.html`: LINES OF DEBATE and SIGNAL vs NOISE
    blocks removed entirely.
  - Storage savings: ~200 bytes/digest. Visual savings: two UI
    sections per digest page.

- **`--top` default 5 → 7.** Two more stories per digest, more
  headroom for the off-beat / singleton picks that 3.7.1 is trying
  to surface. Cap is still 10 (matches README's "5–10 substantive
  narrative stories" range). Updated: argparse default, docstring,
  README example, CLAUDE.md example, systemd units
  (`herald-digest-morning.service`, `herald-digest-afternoon.service`),
  README + CLAUDE.md tunables tables (added `--top` row).

**Hard constraints unchanged:** source-grounding rules (`[post:ID]`
on every claim, verbatim quotes with attribution, paraphrase don't
fabricate, no information already established earlier in the digest),
JSON output schema (headline, story, topic, key_quotes, coverage_type,
section), image + link context rendering.

**Verification:** all `herald_*` modules import cleanly; `ast.parse`
on all edited files clean; `test_image_context.py` 8/8 pass;
`test_link_context.py` 16/16 pass; dry-run digest on the live DB
runs end-to-end (466 posts in window → 40 candidates → stub output
rendered); web template renders cleanly with zero `debate-side` /
`svn-grid` classes in the output.

**What we're NOT changing yet (Phase 3.7.2 and beyond):**
- Ranker flattening (3.7.2): next pass, after we see if the new
  voice alone produces better digests.
- Metrics refresh (3.7.3, rewrite of the original "raise bird
  volume" idea): separate pass, after we see if 3.7.1 lands.
- Model swap to a prose-strong LLM: stay on `gpt-oss-120b` until
  3.7.1-3.7.5 are exhausted. Reasoning model may be over-thinking
  on the synthesis path; not certain. Try voice first.
- HERALD_MAX_CANDIDATES: stay at 40. Raising to 60 is a possible
  follow-up to 3.7.1 if we discover the ranker is the bottleneck.

**Files changed:**
- `herald_synthesise.py` (STAGE2_SYSTEM, STAGE1_SYSTEM, stage1 stub)
- `herald_digest.py` (theme-to-posts, storage call, --top default,
  print guard from earlier hotfix)
- `herald_db.py` (upsert_digest_with_stories signature + INSERT)
- `templates/digest.html` (debate + signal-vs-noise blocks removed)
- `README.md`, `CLAUDE.md`, `DESIGN.md` (doco sync)
- `ROADMAP.md` (3.7.1 status flipped to DONE)
- `/etc/systemd/system/herald-digest-{morning,afternoon}.service`
  (--top 5 → --top 7)

---

## 2026-07-06 — Phase 3.6.2: link context expansion

Shipped link-content extraction for outbound links in candidate posts. The
LLM now sees the actual article body — not just the URL string and the
post's surrounding tweet text. No LLM in the middle (per James: "AI summary
of an AI summary" loses grounding); straight HTTP GET → stdlib HTMLParser
→ strip chrome → ship raw text to stage-2.

**What's new:**

- `link_context` cache table mirrors `image_context` / `quote_context` shape:
  one row per canonical URL (tracking-stripped, lowercased host), with
  `fetched_at`, `fetch_status`, `fetch_error`, `content_text`, `content_chars`,
  `content_bytes`. No `model` column — no LLM involved. PK is `canonical_url`
  so duplicate links across many posts collapse to one row.
- `herald_links.py` new module:
  - `fetch_link_text(url)` — one HTTP GET + extraction. Returns a tagged
    dict (never raises). Failure modes mapped: `http`, `timeout`, `url`,
    `too_large`, `decode`, `other`, `empty` (extraction below min chars).
  - `MainContentParser` — stdlib `HTMLParser` subclass. Strips nav/footer/
    scripts/styles/OG tags. Scopes extraction to the article's main
    content container (see "landmarks" below).
  - `_canonicalise_url(url)` — strips `utm_*`, `fbclid`, `gclid`, `ref`,
    `mc_eid`, `igshid`, `_hsenc`, `_hsmi`, `mkt_tok`, `yclid`, `msclkid`,
    `icid`, plus operator-tunable `HERALD_LINK_DROP_QUERY_KEYS` (default
    `src`). Lowercases scheme + host. Preserves path + fragment.
  - `enrich_link_batch(posts, max_fetches)` — collect-time eager fetch,
    same shape as `enrich_media_batch`. Deduped, budget-capped, never
    aborts on per-URL failure.
  - `attach_link_context(candidates)` — digest-time lookup, sets
    `candidate["_link_context"]` to `list[{post_id, url, text}]` and
    `candidate["_link_skipped"]` to `[]` (symmetry with images).
- LINK CONTEXT block in stage-2 prompt, between IMAGE CONTEXT and the
  "Write the story JSON now." footer. Per-story cap
  `HERALD_LINKS_PER_STORY` (default 2) — engagement-priority sort so
  the most-amplified article wins when a cluster has many.
- 9 new `HERALD_LINK_*` env knobs in `herald_env.LOAD_KEYS`:
  - `HERALD_LINKS_PER_STORY=2`
  - `HERALD_LINK_MAX_CHARS_RENDER=4000` — per-link truncation at render
  - `HERALD_LINK_MAX_CHARS_STORE=8000` — per-row DB cap
  - `HERALD_LINK_MAX_BYTES_FETCH=2097152` — bail out on oversize bodies
  - `HERALD_LINK_TIMEOUT=10` — per-fetch seconds
  - `HERALD_LINK_MIN_TEXT_CHARS=50` — below this → treat as empty
  - `HERALD_MAX_LINK_FETCHES_PER_CYCLE=10` — collect-time budget
  - `HERALD_LINK_USER_AGENT` — set a real Mozilla UA
  - `HERALD_LINK_DROP_QUERY_KEYS` — operator-tunable extra drops
- Wiring in `herald_digest.py`: collect-time `enrich_link_batch` after
  `enrich_media_batch`; digest-time `attach_link_context` after
  `attach_media_context`. Both inside `try/except` so a failure in one
  doesn't abort the other.

**Extraction strategy (MainContentParser):**

The parser walks the HTML and emits text from inside one of these
"landmarks" (if found) or falls back to "extract everything except
SKIP_TAGS" if none are present:

- Real landmark tags: `<main>`, `<article>`, `role="main"`
- Article-body class landmarks: divs with class containing
  `RichTextStoryBody`, `RichTextBody`, `StoryPage-lede`, `ArticleBody`,
  `article-body`, `story-body`, `post-content`, `entry-content`, etc.

Skip-class chrome (skipped even inside `<main>`):

- `Carousel*` — photo carousels with duplicate captions
- `StoryPage-lede` / `Page-lead` — lede/title block (duplicate of post text)
- `StoryPage-actions` / `Page-actions` / `ActionBar` / `ActionLink` —
  share button text
- `Author-bio` / `Author-content` — author bio at article end
- `Promo*` — promotional content
- `PageListRightRail*` / `Sidebar` / `Related` — sidebar widgets

Skip-class check runs BEFORE article-body check so a div with
`class="Author-bio RichTextBody"` (AP News author block) is correctly
skipped rather than mistaken for the article body.

On the FIRST landmark encountered, any text captured before it is
discarded (page `<title>`, meta description, etc.). Stack-based landmark
tracking — push tag on open, pop on close — so div-based landmarks work
correctly without the matching-close ambiguity that depth counters have.

**Bugs found + fixed during dev (test_link_context.py is the regression
test):**

1. **First implementation walked the whole document, capturing pre-`<main>`
   chrome.** Saturated the 8000-char cap with nav menus and section
   headers before reaching the article body. Fix: scope extraction to
   inside the first landmark; flush pre-landmark text.

2. **`<main>` alone wasn't enough for AP News.** AP wraps the whole page
   in `<main>` including nav and sidebars; the article body lives in
   `<div class="RichTextStoryBody RichTextBody">`. Fix: add article-body
   class patterns as sub-landmarks.

3. **Photo carousel chrome.** AP News' `<main>` contains a 5-photo
   carousel with ~250-char captions × 2 layers (slide + overlay) =
   ~2500 chars of duplicate caption text eating the cap. Fix: skip-class
   patterns for `Carousel*`, `Page-actions*`, `Author-bio`, etc.

**Verified end-to-end:**

- Manual: ran the `herald-compare-story1.py` comparison script for
  digest 21 story 1 (post 2073866321247285454 from @AP, linking to AP
  News Balogun red card coverage). The with-link-context version adds:
  - Referee name (Raphael Claus)
  - Historical precedent (first reversal since 1962)
  - Direct Trump quote with attribution
  - Belgian coach Rudi Garcia quote with attribution
  - Court of Arbitration for Sport legal angle
  - Specific round-of-16 match
  All of these are in the AP News article but in NONE of the 3 source
  X posts.
- Cost: stage-2 call goes from $0.000164 to $0.000308 (+$0.000144 per
  story). Trivial at scale.

**Tests:** `test_link_context.py` 16/16 (Test 1-9 unit coverage; Test 10-12
landmark detection including synthetic chrome-heavy pages, no-landmark
fallback, role=main; Test 13 real AP News HTML regression; Test 14-15
article-body class patterns; Test 16 carousel+lede+author+sidebar skip).
Total: 36/36 across `test_image_context.py` (8) + `test_parser_v2.py` (20) +
`test_link_context.py` (8 new in this phase).

**Files added:** `herald_links.py`, `test_link_context.py`
**Files modified:** `schema.sql`, `herald_db.py`, `herald_synthesise.py`,
`herald_digest.py`, `herald_env.py`, `test_link_context.py`

---

## 2026-07-06 — Project state snapshot

Snapshot for whoever picks up the codebase next. Herald's pipeline
(collect / ingest / pre-filter / two-stage synthesise / web UI / timers)
is unchanged — this entry documents current state, ops quick reference,
and known failure modes so a new agent has everything in one place.

**Production state at snapshot:**
- Last successful digest: **#20** (2026-07-05 20:01 UTC, 16h window,
  5 stories, $0.0089, 91s end-to-end)
- DB: `/var/lib/herald/herald.db` — SQLite WAL, ~3682 posts, healthy
- All four timers armed and waiting
- Web UI live at https://herald.jaaaames.com
- Phase 3, 3.5, 3.5.1, 3.6.1 shipped. Phase 3.6.2 (link context) and
  Phase 4 (multi-source) are next.

**Where to read first** (in this order):
1. `/home/james/srv/herald/README.md` — orientation, status, quick start
2. `/home/james/srv/herald/DESIGN.md` — architecture, schema, source-grounding, decisions
3. `/home/james/srv/herald/ROADMAP.md` — phases, what's done, what's next
4. `/home/james/srv/herald/CHANGELOG.md` — dated history (this file)

**Operational quick reference (all on optiplex as user `james`):**

```bash
# Manual collect
cd ~/srv/herald && python3 herald_digest.py collect

# Manual digest (real LLM call, ~90s, ~$0.009)
cd ~/srv/herald && python3 herald_digest.py digest --since "12h ago" --top 5 --max-candidates 40

# Dry-run (no LLM, free)
cd ~/srv/herald && python3 herald_digest.py digest --since "12h ago" --dry-run --show

# Pipeline health (last 24h, grouped by step+status)
ssh optiplex "sqlite3 /var/lib/herald/herald.db \"SELECT step, status, COUNT(*) FROM pipeline_runs WHERE started_at > strftime('%s','now','-1 day') GROUP BY step, status\""

# Timer status
ssh optiplex "systemctl status herald-collect.timer herald-digest-morning.timer herald-digest-afternoon.timer --no-pager"

# Tail digest log
ssh optiplex "tail -50 /var/log/herald/digest.log"
```

**Open decisions waiting on James:**
1. **Stage 2 model default**: switch from `openai/gpt-oss-120b` to
   `google/gemini-2.5-flash` so the LLM uses image context (Phase 3.6.1).
   Trade-off: ~5x cost per story, still trivial at scale.
2. **Phase 3.6.2 (link context)**: scoped but not started. Raw-text
   scraping (article body cleaned up, no OG tags, no LLM summary).
3. **Phase 4 (multi-source)**: RSS collector first, then news API.

**Failure modes to know about:**
- Bird CLI requires Node >= 22. Optiplex has Node 18 system + 26 via nvm.
  Always use `/home/james/.local/bin/bird-nvm` wrapper — never the
  system bird (`/usr/local/bin/bird` uses system node and fails with
  "Unexpected token with").
- LLM transient failures (finish_reason=error, empty content, zero
  cost) are now logged loudly in digest.log but do not crash the
  pipeline. Will result in empty digest for that cycle.
- Failed context fetches (fetch_status=failed) are sticky. Manual
  retry: delete rows from quote_context / conversation_context /
  image_context where fetch_status=failed.
- WAL mode on SQLite (Phase 3.5 fix) means concurrent connections
  no longer block. Do not run herald_digest.py collect and digest
  at the same time by accident — they do not corrupt, but you will
  get confusing interleaved log lines.

**Test entry points** (no live Twitter needed):

```bash
cd ~/srv/herald
HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_parser_v2.py     # 20 cases
HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_image_context.py # 8 cases
HERALD_DB_PATH=/tmp/herald-test/test.db ./test_mock_bird --help
```

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
  (`~/tmp/newsroom/`, the precursor project), especially
  `SYSTEM.md`, `schema.md`, `bird_parse.py`, `bird_context.py`. Key
  finding: previous failure was caused by agent-driven cron jobs running
  batch LLM synthesis — confirmed "maxConcurrentRuns too high →
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
  `~/tmp/newsroom/feed/` (~12 JSONL
  files, ~36 hours of X data) can drive Phase 0 development without
  needing live `bird` auth.
- Reference sample digest.
  `~/tmp/newsroom/archive/2026-05-13_0600_digest.md`
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

## 2026-07-05 — Phase 3.6.1: image context expansion

Shipped vision-LLM context for photo media attached to posts. Video and
GIF media skipped (per James, futile to glean from thumbnails, too
expensive to extract frames).

**What's new:**

- `image_context` cache table mirrors `quote_context`: one row per media
  URL, with `description` from the vision LLM and the `model` that
  produced it. Stable URLs (pbs.twimg.com is content-addressable) so
  descriptions are fetched once per image, ever.
- `herald_media.py` module: `fetch_image_description`, `enrich_media_batch`
  (collect-time eager, budgeted via `HERALD_MAX_MEDIA_FETCHES_PER_CYCLE`,
  default 10), `attach_media_context` (digest-time lookup).
- IMAGE CONTEXT block in stage 2 prompt: one line per image, prefix
  `🖼`, cite with `[post:ID]`. Capped at `HERALD_IMAGES_PER_STORY=1`
  default; engagement-priority picker so the highest-liked post's image
  wins when a cluster has multiple.
- Default vision model `openai/gpt-4o-mini` via OpenRouter (override
  via `HERALD_VISION_MODEL`). Roughly $0.00015 per low-res image.
- pbs.twimg.com URLs go straight in as `image_url` content — no
  download/base64 needed (OpenRouter accepts https URLs directly).

**Tests:** 8 new in `test_image_context.py`. Total 28/28 across both
suites (8 image + 20 existing parser v2).

**Files touched:** `schema.sql`, `herald_db.py`, `herald_synthesise.py`,
`herald_digest.py` (collect + digest wiring). New: `herald_media.py`,
`test_image_context.py`.

**What's next:** Phase 3.6.2 (link context) — raw-text article scraping,
no LLM in the middle, no Open Graph tags. Separate pass.
