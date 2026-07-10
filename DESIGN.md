# Herald — Design Spec

## Summary

Herald is a twice-daily distilled read of the X/Twitter home timeline (and,
later, other sources). It ingests the timeline on a schedule, groups posts
into stories, ranks them, and writes the top 5–10 into a self-contained
15-minute digest via LLM — multi-paragraph narrative stories with embedded
quotes and inline source citations. Delivered through a web UI on
`herald.jaaaames.com`. No email push, no feedback system, no status page —
just a URL James opens twice a day.

The design is deliberately minimal: as few moving parts as possible. The
complexity lives in synthesis quality and failure-mode handling, not in the
user-facing surface area.

Two prior attempts at this failed by "lacking consistency in producing an
ongoing digest" — the previous Newsroom v4 code at
`~/tmp/newsroom/` (the precursor project) confirmed the root
cause: agent-driven cron jobs running batch LLM synthesis through a
context-window-based harness, overloading `maxConcurrentRuns` and producing
"AI service temporarily overloaded" failures. Herald addresses this by
routing synthesis directly through OpenRouter, completely outside the
agent's context window.

## Motivation

### The consumption problem

The current X consumption pattern is hours of unbounded scrolling per day,
driven by algorithmic curation optimised for engagement rather than signal.
The output is FOMO, anxiety, and the persistent feeling of having not read
enough — even after hours of scrolling.

The target pattern is two sit-down reading sessions per day, each ~15
minutes, on a fixed schedule. After the second session, the day is over —
there is no "let me just check once more."

### The sovereignty angle

The X feed is one of the largest remaining unmanaged information inputs in
James's life. The web browser, email, calendars, password manager, and
Bitcoin stack are sovereign. The X feed is not — it lives on Elon Musk's
servers, subject to algorithm changes, ToS changes, deplatforming risk, and
ad-driven incentive misalignment.

Herald reclaims this: raw feed captured and stored on personal
infrastructure; synthesis done with an LLM accessed via OpenRouter (model
choice is sovereign); interface owned end-to-end. X becomes a publishing
surface, not a consumption surface.

## Goal & Success Criteria

### Functional
- A 15-minute digest, twice a day, covering the previous 8–16 hours of
  activity in the X home timeline
- Each digest: 5–10 substantive narrative stories, each built from one or
  more source posts
- A story is *the product* — multi-paragraph narrative synthesis with
  editorial framing, embedded direct quotes with attribution, source
  citations inline, and quantitative context where useful
- **Self-contained.** Reading the digest = reading the X feed. Source
  posts are cited for verification, but the narrative lives in the digest
  itself. No "click through to X to get the story."
- Stories still developing across digests are tagged as follow-ups with
  explicit continuity metadata — no "breaking news" treatment of ongoing
  events

### Quality
- Useful on day 1, no feedback history needed (feed is already curated)
- Resists the infinite-scroll trap: no infinite scroll, no FOMO surface,
  clear "you are caught up" state

### Reliability
- Reliably produces digests without babysitting
- Degrades gracefully: if X returns nothing, still serve yesterday's
  digest; if LLM is down, fall back to extractive ranked-list digest
- Structured logging + `pipeline_runs` table for visibility

### Sovereignty
- Raw posts and digests stored locally on optiplex, exportable, owned
- LLM choice via OpenRouter (model-swappable); cost is bounded; no lock-in
- X is a publishing surface; Herald owns the consumption surface
- Herald runs standalone on optiplex — different runtime, different
  machine, different lifecycle from anything else on the laptop

## Stack

| Layer | Technology | Notes |
|---|---|---|
| Host | **optiplex** | Always on, serves jaaaames.com. Herald runs here standalone. |
| Upstream collector | `bird` CLI v0.8.0 + Python parser | `bird` installed at `~/.npm-global/bin/bird`; needs install on optiplex too. Auth via Twitter `auth_token` + `ct0` cookies. |
| Parser | `parser.py` (Herald's fork of newsroom's `bird_parse.py`) | Three fixes vs prior iteration: media URLs (now populated), parent_tweet stub (text=null, fetched on demand in Phase 3), note_tweet long-form extraction. |
| Database | SQLite (single file) | `/var/lib/herald/herald.db`. Schema is source-agnostic. |
| Pre-filter | Embeddings + cosine cluster | OpenRouter `text-embedding-3-small`. Threshold 0.50. Picks ~40 representative candidates from window. |
| Ranking | Hand-tuned weighted score | `size × engagement × log10(followers+10)`. No learned weights; no feedback loop. |
| LLM (synthesis) | OpenRouter, model-swappable | Current: `openai/gpt-oss-120b` (reasoning model). Stage-1 timeline map + per-story stage-2 calls. |
| Web UI | Python Flask + gunicorn on 127.0.0.1:8060 | Bootstrap-free, dark theme matching jaaaames.com aesthetic. Reverse-proxied by nginx. |
| Server | Nginx + gunicorn | Native systemd service, no Docker. |
| Auth | None (web UI is public) | Single user, single machine, single domain. |
| Storage | `/var/lib/herald/herald.db` on optiplex | Single SQLite file, backed up via optiplex regime. |
| Source-agnostic schema | `posts.source` field + generic `post_type` | X is the first collector; future collectors (RSS, news API) drop in unchanged downstream. |

### LLM cost (current)

`gpt-oss-120b` is a **reasoning model** — it spends reasoning_tokens that
count toward `completion_tokens`. Stage-1 (timeline map) typically 5k input
+ 3–4k output. Stage-2 (per story) ~1k input + ~1k output. Per-call budget
(`HERALD_STAGE{1,2}_MAX_TOKENS`) is 64000 to give the reasoning room.

Approximate cost: see CHANGELOG 2026-06-28 for the baseline run. Per-digest
cost is ~$0.05–0.15 on the current model.

### LLM cost estimate (cheap-tier reference)

~50 stories per digest × 2 digests/day × ~1000 tokens per story (input +
output blended):
- **Cheap tier** ($0.30/M blended): 100 stories × 1000 tok × $0.30/M = **$0.03/day ≈ $0.90/month**
- **Mid tier** ($5/M blended): ~**$0.50/day ≈ $15/month**

Embeddings are negligible (~$5/year at current volumes).

## Architecture

```
   optiplex
   ────────
          │
          ▼
   ┌───────────────┐
   │   Collector   │   cron + bird CLI + parser
   │  (scheduled)  │   POSTs validated posts to ingest API
   └───────┬───────┘
           │  parsed JSONL → ingest
           ▼
   ┌───────────────┐
   │   SQLite DB   │   posts, accounts, stories,
   │  (single file)│   story_posts, digests, pipeline_runs
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │  Pre-filter   │   embed (text-embedding-3-small) + cosine cluster
   │ (embed+cluster)│  rank by size × engagement × log10(followers+10)
   └───────┬───────┘
           │  ~40 candidates
           ▼
   ┌───────────────┐
   │  Stage 1 LLM  │   One call. Sees all 40 candidates.
   │ (timeline map)│   Outputs: zeitgeist themes,
   └───────┬───────┘   stories_to_write (with angles) (Phase 3.7.1: debate_lines + signal_vs_noise dropped)
           │
           ▼  one call per story (max 7, Phase 3.7.1)
   ┌───────────────┐
   │  Stage 2 LLM  │   Each sees only its theme's posts.
   │ (per-story)   │   Writes 1–3 paragraph narrative with
   └───────┬───────┘   inline [post:ID] citations, embedded quotes
           │
           ▼
   ┌───────────────┐
   │ Digest        │   Persists digest row + N story rows
   │ assembler     │   (timeline_map, zeitgeist, input_post_ids;
   └───────┬───────┘   debate_lines + signal_vs_noise removed Phase 3.7.1)
           │
           ▼
   ┌───────────────┐
   │   Web UI      │   herald.jaaaames.com
   │  (gunicorn)   │   Flask + Bootstrap-free dark theme
   └───────────────┘

   parallel:
   ┌─────────────────┐
   │   Debug log     │   /var/log/herald/*
   │   pipeline_runs │   Last-run status of all systems
   │   table in DB   │
   └─────────────────┘
```

### Components

| # | Component | Purpose | Failure mode handled |
|---|---|---|---|
| 1 | **Collector** | cron-scheduled wrapper around `bird`; pipes through parser; ingests to DB | Bird fails → log + skip; downstream still works on existing posts |
| 2 | **Ingest** | Validates, deduplicates by `(source, external_id)`, stores | Schema drift → reject + log; existing posts unaffected |
| 3 | **Pre-filter** | Embedding + cosine cluster + rank | Too many singletons → fall back to top-N |
| 4 | **Stage 1 (timeline map)** | Editorial survey across all candidates. Outputs zeitgeist themes + stories_to_write (with angles). debate_lines + signal_vs_noise removed in Phase 3.7.1. | LLM down → extractive fallback |
| 5 | **Stage 2 (per-story)** | One LLM call per story with its theme's posts | Validation fail → retry with stricter prompt → extractive fallback |
| 6 | **Digest assembler** | Persists digest + story rows | Always succeeds (input is bounded) |
| 7 | **Web UI** | Only delivery surface. `herald.jaaaames.com`. Lists digests chronologically; each story shows source citations inline; images hotlinked; "view sources" expansion | Self-hosted; same uptime as optiplex |
| 8 | **Debug log** | `/var/log/herald/*` + `pipeline_runs` table | Read on demand |

### Schedule (systemd timers on optiplex, all UTC)

| Timer | When (UTC) | When (AEST) | Action | Window |
|---|---|---|---|---|
| `herald-collect.timer` | every 30min during 12:00–20:00; every 2h off-peak | (= AEST 22:00–06:00 peak = US EDT 08:00–16:00) | `herald_digest.py collect` | n/a |
| `herald-digest-morning.timer` | 20:00 UTC | 06:00 AEST | `digest --since "16h ago"` | 14:00 prev day → 06:00 today |
| `herald-digest-afternoon.timer` | 04:00 UTC | 14:00 AEST | `digest --since "8h ago"` | 06:00 → 14:00 today |

16h + 8h windows tile cleanly across 24h, no overlap, no gap.

### Run idempotency

Every pipeline step is keyed and re-runnable:

- Collector: an in-flight fetch blocks a duplicate (lock file or DB flag)
- Ingest: deduplication by `external_id`; safe to ingest the same post twice
- Pre-filter: regenerates candidate list from current posts; safe to re-run
- Stage 1 + Stage 2: re-callable for a given digest; result stored keyed by
  `(digest_id, model_version, prompt_version)`
- Stories are immutable once assigned to a digest

## Storage

Single SQLite file at `/var/lib/herald/herald.db`. Schema is
**source-agnostic** — `posts.source` tracks origin; the clusterer, ranker,
summarizer, and digest assembler treat all sources uniformly.

```sql
posts (id, external_id, source, author_id, author_handle,
       created_at, ingested_at, text, media_urls, link_urls,
       metrics, post_type, raw,
       UNIQUE (source, external_id))

post_embeddings (post_id, model, embedding, embedded_at)
  -- populated by herald_embed

accounts (id, source, handle, display_name, bio, follower_count, verified,
          first_seen, last_seen, notes, PRIMARY KEY (source, id))

stories (id, digest_id, headline, story, key_quotes, media_references,
         topic, tags, coverage_type, continuity_story_id, what_changed,
         created_at, updated_at, post_count, total_score, cluster_method,
         -- two-stage synthesis (added 2026-06-28)
         section, theme_label, angle,
         -- per-story cost tracking (added 2026-06-28)
         prompt_tokens, completion_tokens, cost_usd)

story_posts (story_id, post_id, relevance, PRIMARY KEY (story_id, post_id))

digests (id, period_start, period_end, generated_at,
         duration_estimate_minutes, story_count, topics, sources,
         model_versions,
         -- two-stage outputs (added 2026-06-28; debate_lines + signal_vs_noise
         -- columns retained for back-compat but no longer written, Phase 3.7.1)
         zeitgeist, debate_lines, signal_vs_noise, timeline_map,
         input_post_ids, window_hours)

pipeline_runs (id, started_at, finished_at, step, source, status, error,
               rows_in, rows_out,
               prompt_tokens, completion_tokens, cost_usd, model)
```

### Volume & retention

- ~500–1000 posts/day × 365 ≈ 200K–400K posts/year
- ~1–2 KB per post → ~200–400 MB raw/year
- **Default retention: 90 days for raw posts.** Digests and account
  metadata kept forever. Raw posts can be re-pulled from source if needed.
- Raw retention can be tightened to 7 days if cost/footprint becomes a
  concern; this is a config knob.

### Story continuity

The clusterer and summarizer may look back at recent digests to recognise
ongoing stories and tag them as follow-ups rather than fresh events. This
requires more input data for the LLM (relevant previous digest summary
provided in the prompt). Phase 2 stretch goal — cost trade-off
acknowledged. Start with single-digest-window only.

## Source-Grounding Requirement (critical constraint)

The previous attempts' LLM was synthesising too much, putting words in
people's mouths, and not providing full context. The source-grounding
requirement is non-negotiable in Herald — and because the digest is a
*narrative product* (not a list of links), the constraint has to be
enforced at the prose level, not just at a structured-claims level.

### Hard requirements

1. **Every factual claim in a story's narrative must cite a source post
   inline.** Format: `[post:1234567890]` (X tweet ID) or
   `[post:rss:<guid>]` for future RSS sources. Resolves to the original
   post URL on hover/click. No bare assertions.
2. **No synthesis beyond what the source posts actually say.** Paraphrasing
   is fine. Fabrication is not. If the source posts don't establish
   something, the story doesn't say it.
3. **Direct quotes from source posts are embedded in the narrative with
   attribution.** Format: `"quote text" — @author`. The `key_quotes` array
   in the LLM output tracks which quotes appear in the story and where
   they came from.
4. **Images are rendered inline.** Hotlinked from the source. Critical for
   context — many X posts are image-only or image-primary; news articles
   often lead with an image.
5. **The UI surfaces the full source set on demand.** Each story has a
   "view sources" expansion showing every contributing post, with
   provenance (source, author, timestamp, original URL).

### Validation

If validation fails:
- Log the failure (including the rejected output and validation errors) to
  `pipeline_runs`.
- Retry once with stricter prompt.
- If still failing, fall back to extractive digest for this story (no AI
  narrative — just the highest-engagement source posts with attribution)
  and surface in the digest UI as "no AI narrative available".

### Why this matters

Without source grounding, the digest becomes a third-party interpretation
of what X people said — and given that the whole point of Herald is to hear
what *those specific people* actually said, fabrication is catastrophic.
The constraint also makes fact-checking trivial: every claim maps back to
source material.

The inline-citation format (vs. a separate claims array) is chosen because
the digest is *narrative prose*. A separate claims array would create a
two-track structure (prose + claims) that drifts. Inline citations keep
the narrative and the evidence in lockstep — what the story says, the
citations show.

## Context Expansion (parser v2 shipped 2026-07-03; image + link 2026-07-05/06)

Replies in Herald's digest window are often the **bottom half** of a thread:
James sees the reply, but not what was being replied to. Bird's `--json-full`
inlines the reply's own content but never inlines the parent. Before parser v2,
the parser emitted a stub `parent_tweet` (id + url, text=null) and stopped there.

Parser v2 adds **eager thread/quote-context fetching and caching**:

```
   collect (every 30min peak / 2h off-peak)
   ───────
   bird home --json-full
     ↓
   collect.py   ← RT resolution, age gate
     ↓
   parser.py    ← normalise to JSONL
     ↓
   herald_context.enrich_batch       ← (NEW) bird thread / bird read,
                for each reply not yet cached, fetch the full thread
                parse with herald parser, write to conversation_context
                for each quote with no inlined target, fetch the parent
                write to quote_context
     ↓
   herald_ingest.py   ← posts to DB; conversation/quote fields persisted
                so synthesis can look up cached context by conversation_id

   digest (06:00 / 14:00 AEST)
   ───────
   pull posts in window
     ↓
   herald_embed.select_candidates     ← pre-filter to ~40 posts
     ↓
   herald_context.attach_context_to_candidates     ← (NEW) DB lookup by
                conversation_id / quoted_tweet_id, attach as
                candidate._thread_context / candidate._quote_context
     ↓
   herald_synthesise.stage1_timeline_map  ← sees candidates + context
   herald_synthesise.stage2_story         ← sees theme's posts + context
```

Phase 3.6 (image + link context, shipped 2026-07-05/06) extends the same
pattern: at collect time, `herald_media.enrich_media_batch` fetches
vision-LLM descriptions of attached photos (cached in `image_context`);
`herald_links.enrich_link_batch` fetches the actual article body of each
outbound link via stdlib `HTMLParser` with no LLM in the middle (cached
in `link_context`). At digest time, `attach_media_context` and
`attach_link_context` look up the cache and attach `_image_description` /
`_link_context` to candidates. Stage 2 then renders `IMAGE CONTEXT`
(🖼 marker, ≤400 chars per photo) and `LINK CONTEXT` (📰 marker, URL
visible, ≤4000 chars per article) blocks between `CONTEXT POSTS` and the
"Write the story JSON now." footer. Both are scoped to a per-story cap
with engagement-priority picker.

### Why eager, not on-demand

The digest at 06:00 / 14:00 AEST is time-sensitive. Eager fetches happen
during `collect` runs which have headroom (they fire every 30 min during
peak, every 2 h off-peak). The same conversation can be referenced by many
posts across many collects — caching at the conversation_id level collapses
duplicates. Per-post failures are recorded as `fetch_status='failed'` rows
so we don't repeatedly retry a tweet that bird keeps rejecting (manual
`DELETE` from `conversation_context` to force a retry).

### Tables

```sql
conversation_context (
  conversation_id TEXT PRIMARY KEY,    -- X conversation id
  fetched_at      INTEGER NOT NULL,
  fetch_status    TEXT NOT NULL,       -- 'success' | 'failed'
  fetch_error     TEXT,                -- error message if status='failed'
  thread_json     TEXT NOT NULL,       -- JSON array of parsed tweets
  tweet_count     INTEGER NOT NULL,
  source_tweet_id TEXT                 -- which of our posts triggered the fetch
)

quote_context (
  quoted_tweet_id TEXT PRIMARY KEY,
  fetched_at      INTEGER NOT NULL,
  fetch_status    TEXT NOT NULL,
  fetch_error     TEXT,
  data            TEXT NOT NULL        -- one parsed tweet
)

image_context (
  media_url     TEXT PRIMARY KEY,     -- pbs.twimg.com URL; content-addressable
  fetched_at    INTEGER NOT NULL,
  fetch_status  TEXT NOT NULL,
  fetch_error   TEXT,
  description   TEXT,                 -- vision-model caption
  model         TEXT                  -- which vision model produced it
)

link_context (
  canonical_url TEXT PRIMARY KEY,     -- tracking-stripped, lowercased host
  fetched_at    INTEGER NOT NULL,
  fetch_status  TEXT NOT NULL,
  fetch_error   TEXT,
  content_text  TEXT,                 -- extracted article body, ≤8000 chars
  content_chars INTEGER,              -- len(content_text)
  content_bytes INTEGER               -- len(original HTTP body)
)
```

Plus three columns added to `posts` for round-trip via `upsert_post` /
`get_posts_in_window`: `conversation_id`, `quoted_tweet_id`, `in_reply_to_id`.

### Limits

`HERALD_CONTEXT_MAX_FETCHES` (default 30) caps the per-collect cost. When
reached, additional conversations / quote parents are recorded as 'skipped'.
30 is plenty for a 75-tweet home pull where ~30–50% are replies and
~half hit unique conversations.

`HERALD_MAX_MEDIA_FETCHES_PER_CYCLE` (default 10) caps vision-LLM calls
per collect. `HERALD_MAX_LINK_FETCHES_PER_CYCLE` (default 10) caps
article-body fetches per collect. All three share a pool of "background
HTTP work" the collect cycle can absorb without extending its tail latency
materially.

### LLM prompt changes

Stages 1 and 2 each prepend a `CONTEXT POSTS` block to the user message
when candidates carry context. Each context post is rendered as
`[post:<id>] @<author>: <text>` (truncated to 600 chars). The LLM is told
context posts are eligible for citation with the same `[post:ID]` convention
and may ground claims against them.

Stage 2 also appends an `IMAGE CONTEXT` block (one line per attached photo,
🖼 marker, ≤400 chars per description) and a `LINK CONTEXT` block (one
line per linked article, 📰 marker, URL visible, ≤4000 chars per article).
Both are sourced from the post itself (inbound context), not external
research (which is a future phase). Image context uses a vision LLM;
link context uses stdlib `HTMLParser` with no LLM in the middle (per James:
"AI summary of an AI summary" loses grounding).

### Link context extraction strategy

`MainContentParser` in `herald_links.py` walks the page HTML and emits
text from inside a "landmark" container, if found, or falls back to
"extract everything except skip tags" if none:

- **Real landmark tags**: `<main>`, `<article>`, `role="main"`
- **Article-body class landmarks**: divs with class containing
  `RichTextStoryBody`, `RichTextBody`, `StoryPage-lede`, `ArticleBody`,
  `article-body`, `story-body`, `post-content`, `entry-content`, etc.

Skip-class chrome (skipped even inside `<main>`): `Carousel*`,
`StoryPage-lede`, `Page-lead`, `StoryPage-actions`, `Page-actions`,
`ActionBar`, `ActionLink`, `Author-bio`, `Promo`, `PageListRightRail*`,
`Sidebar`, `Related`. Skip-class check runs BEFORE article-body check so
a div with `class="Author-bio RichTextBody"` is correctly skipped rather
than mistaken for the article body. On the first landmark encountered, any
text captured before it is discarded (page `<title>`, meta description).

The list is hand-curated from real sites we've hit; expect to add more
patterns as new sites appear. Framework is general; specific patterns are
not.

## Multi-Source Architecture (designed in from start, deferred to ROADMAP Phase 4)

### The framing metaphor

The X feed is *commentary*. Most accounts James follows are not
journalists; they're people commenting on news, events, and other
commentary. The digest is therefore a digest of commentary — useful, but
missing the factual baseline that the commentary is responding to.

The eventual design folds in **news sources** (RSS, news APIs, Mastodon,
Substack, etc.) as a **factual baseline layer**. The X commentary is then
*framed* by the news: "X is talking about Y; here's what Y actually is."

### Source-agnostic from start

The `posts` schema includes a `source` field (defaulting to `'x'` for v1).
The clusterer, ranker, summarizer, and digest assembler treat all sources
uniformly — they operate on the `posts` table without knowing or caring
which source produced the post. Adding a new source in Phase 4 means
writing a new collector that drops posts into the same table. Everything
downstream is unchanged.

```
source A collector (X, bird + parser)  ──┐
                                         ├──► posts table (source-agnostic) ──► Herald pipeline
source B collector (RSS, future)        ──┤
                                         │
source C collector (news API, future)    ──┘
```

### Status

Phase 4 in the roadmap; not built until Herald-X is reliable. Herald IS
the news-aggregation project — the existing `horizon/ideas/news-aggregation.md`
(Miniflux/RSS) is a *prior concept* that Herald subsumes, not a parallel
project.

## Failure Modes Designed Against

The two prior attempts failed by "lacking consistency in producing an
ongoing digest." The symptom is clear: the pipeline stopped producing
digests and recovery required manual intervention.

### Specific failure modes & mitigations

| Failure mode | Mitigation |
|---|---|
| X returns nothing / `bird` errors | Log; downstream still works on existing posts; produce a digest (possibly thin) |
| X rate-limits / IP-bans the collector | Detect via "zero new posts in N hours"; reduce collection frequency |
| Schema drift in X / `bird` output | Schema validation on every post; reject with logged error |
| LLM rate-limited or down | Retry with backoff; if still failing, fall back to extractive ranked-list digest |
| LLM hallucinates claims | Source-grounding validation; retry with stricter prompt; fall back to extractive |
| Disk full / DB corruption | Health check on each pipeline run; size monitoring; backups via optiplex regime |
| Web UI down (nginx dies) | Debug log visible via SSH; restart systemd service |
| Whole thing silently dies | Structured logs + `pipeline_runs` table; James reads when curious |

### No feedback system (by design)

The feed is already curated. Every account James follows is already
vouched for. A feedback system adds moving parts without proportionate
benefit — the ranker doesn't get smarter because James clicks thumbs-up;
it just gets a per-account bias that overrides the feed-curation decision
he already made.

Mitigation for "small accounts get drowned out": a hand-tuned ranker that
normalises by `follower_count` (divide engagement by `log(follower_count + 10)`
in the cluster ranker). This is a math problem, not a feedback problem.
Tune the weights until smaller accounts surface proportionally.

### No watchdog (by design)

A watchdog that pings James when something breaks is out of scope:

- Herald is a personal tool, opened twice a day. The lag between "system
  broke" and "James notices" is bounded by the next 6am/2pm press.
- Watchdog spam is worse than silent failure for a sovereign tool — alerts
  desensitise the user to real problems.
- Structured logs + `pipeline_runs` give James enough to debug when he
  checks.

If the failure rate turns out to be higher than expected (e.g. once a week),
revisit. Not pre-emptively.

## Out of Scope (v1)

- Real-time updates — this is a deliberate consumption ritual, not a stream
- Posting to X — separate problem
- DMs — separate problem with different needs
- Discovery / trending / explore — only your network matters
- Search over historical X — defer
- Multi-account — defer
- Native mobile app — web UI is mobile-friendly via PWA
- **Feedback system** — feed is already curated; no per-account weights,
  no interest/noise buttons, no snooze UI. Hand-tuned ranker with
  normalised follower count is the lever.
- **Email delivery** — web UI is the only surface. James knows the schedule.
- **Watchdog / alerting** — debug log + `pipeline_runs` table is the
  visibility mechanism. James reads it when curious.
- **Deep dives / context gathering** — stretch (ROADMAP Phase 6); v1 ships
  without it.
- **Accumulation-based publishing** — deferred (ROADMAP Phase 5); v1 is
  time-based twice-daily.

## Decisions Made (with rationale)

- **Herald, not "x-feed-digest"** — short, evocative, fits sovereign
  naming convention; "herald" = one who reads the news aloud
- **Herald runs on optiplex** — always on, serves jaaaames.com.
  Standalone service with its own runtime, machine, and lifecycle.
- **6am + 2pm AEST** — frames content against US Eastern business hours
- **`bird` CLI as upstream** — proven tool from prior iterations; not a
  rebuild
- **OpenRouter, not agent context** — Herald's batch pipeline must not
  overload the agent's conversational context window; OpenRouter gives
  model-swappable access
- **`gpt-oss-120b` as current default** — reasoning model earns its keep
  on stage-1 timeline reasoning and stage-2 narrative synthesis; cheap
  but not free
- **Source-grounding as hard constraint** — previous attempts'
  hallucination failure mode; every claim must trace to a source post
- **Digest is self-contained narrative, not gateway** — each story is a
  synthesised narrative (1–3 paragraphs) with embedded quotes and inline
  `[post:ID]` citations; reader doesn't need to click through to X
- **5–10 stories per digest** — substantive coverage, not summary spam. Default 7 (Phase 3.7.1; raised from 5).
- **Editorial register: digital-native tech magazine** (Phase 3.7.1) —
  closer in tone to Daring Fireball, The Verge's feature pages, or
  Rest of World than to a news brief. Single-post stories are welcome,
  off-beat and weird moments are welcome on their own merits, humour
  is welcome when the source posts are joking. The source-grounding
  rules remain hard; voice is not.
- **Stories abstraction, not ranked feed** — synthesis is what makes
  15-min reading possible; curation alone isn't enough
- **SQLite, no Postgres** — single-user scale, matches sovereign-stack
  preference
- **No Docker, native systemd** — consistent with comstac, SPENCE, FORGE
- **Web UI is the only delivery surface** — no email push, no push
  notifications. James opens the URL.
- **No auth on web UI** — single user, single machine, single domain;
  auth adds nothing
- **Source-agnostic schema from start** — `posts.source` field, generic
  `post_type`. X is the first collector; future collectors drop in
  unchanged.
- **Hand-tuned ranker with normalised follower count** — no learned
  weights, no feedback loop. Smaller accounts surface via
  `log(follower_count)` normalisation in the score function.
- **Embedding threshold 0.50** — tuned against real X data (0.65 was too
  strict, produced 0 multi-post clusters; 0.50 produces ~3 multi-post
  clusters per 41 posts)
- **No feedback system** — feed is already curated; feedback adds moving
  parts without proportionate benefit
- **No email** — James knows the schedule; web UI is enough
- **No watchdog, no status page** — debug log + `pipeline_runs` table is
  the visibility mechanism. James reads it when curious.
- **Two-stage synthesis (timeline-aware)** — stage 1 sees all candidates,
  stage 2 sees only its theme's posts. Editorial reasoning across the
  timeline, not per-cluster regurgitation
- **Embedding-based pre-filter** — keeps each LLM call's context bounded
  (~5k input for stage 1, ~1k for stage 2), so we can have ~40 candidates
  in stage 1 without context explosion
- **90-day raw retention** — enough for story continuity (Phase 2 stretch);
  can tighten to 7 days if needed
- **Multi-source deferred to ROADMAP Phase 4** — but designed in from the
  start (collector-specific; pipeline source-uniform)
- **Parser v2: eager context fetching during collect, not on-demand at
  digest time** — collect runs in batches with headroom; the digest window
  (06:00 / 14:00 AEST) is time-sensitive and shouldn't wait on Twitter.
  Caching at the conversation_id level collapses duplicate fetches
  across batches; failures are recorded so we don't retry endlessly.
- **Accumulation-based publishing deferred to ROADMAP Phase 5** —
  depends on having a factual baseline layer (Phase 4 multi-source)
- **`coverage_type` on stories** (`one_shot` | `ongoing`) — distinguishes
  ephemeral stories from tracked ones; controls cleanup behaviour
- **Continuity metadata on stories** (`continuity_story_id`,
  `what_changed`) — structural follow-up tagging; replaces ad-hoc
  "Stories still developing" lists
- **Inherit `bird_parse.py` from prior iteration** — fix the gaps (media
  URLs, parent_tweet text, note_tweet), don't rewrite
- **Deep dives / context gathering** — stretch (Phase 6); revisit only
  after Phase 4 ships

## Open Questions

1. **Parser v2 design (next item, ROADMAP Phase 3).** Ingest parent tweets
   of replies on demand vs. always-eager? Thread expansion: full chain or
   window of N most recent? Fetch depth per chain (1, 2, configurable)?
2. **Story continuity activation.** When do we enable `continuity_story_id`
   lookup across digests? Cost trade-off: each story prompt grows by
   ~the previous digest summary. Defer until prompts are otherwise stable.
3. **Ranker follower-count normalisation.** Specific formula is a tuning
   question. Default to `log(follower_count + 10)` in pre-filter rank;
   adjust as data accumulates.
4. **Per-story-type prompts.** News event vs. discussion vs. announcement
   vs. personal observation. Phase 2 stretch.
5. **Embedding model swap.** `text-embedding-3-small` works; larger
   models (e.g., 3-large) might give better cluster quality. Cost vs.
   benefit — defer.
6. **OpenRouter key rotation.** Quarterly rotation, standard practice.
   Confirm during Phase 2.
7. **Digest windowing.** 16h + 8h tiles cleanly but is asymmetric (more
   overnight content in morning). Re-evaluate after a few weeks of
   two-stage outputs.

## Relation to Other Projects

| Project | Relationship |
|---|---|
| Bitcoin infrastructure | Not directly involved. |
| SPENCE / FORGE | Reference patterns for the stack (PHP/Go + SQLite + Bootstrap dark theme + HMAC auth). Herald is part of the same sovereign-app family. Herald uses gunicorn + Flask + Bootstrap-free CSS instead — clean-room, not copy-paste. |
| Comstac | Independent now (no email delivery from Herald). Both are sovereign apps on optiplex. |
| jaaaames.com | Herald serves from `herald.jaaaames.com`, joining the jaaaames.com subdomain family served by optiplex nginx. |
| Hardware sovereignty (planned) | When dedicated server arrives, Herald can move there. Until then, optiplex. |
| News aggregation (horizon) | **This project.** Herald IS the news-aggregation project — the Miniflux/RSS concept in `horizon/ideas/news-aggregation.md` has evolved into Herald's LLM-synthesised narrative approach. That file points here. |