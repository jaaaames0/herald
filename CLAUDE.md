# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Herald is

Twice-daily distilled read of the X (Twitter) home timeline. Each digest is 5–10 narrative stories synthesised by an LLM via OpenRouter, with inline source citations (`[post:ID]`) and image context. The digest is the product — readers don't click through to X. See `README.md` for orientation, `DESIGN.md` for the design spec, `ROADMAP.md` for phases, `CHANGELOG.md` for dated history.

Lives on `optiplex`. Public at https://herald.jaaaames.com. No auth (single user, public data).

## Common commands

Development commands run from `/home/james/src/herald`. Production runs from
a root-owned, versioned release below `/usr/local/lib/herald`; never edit that
release in place. The unified CLI is `herald_digest.py`. Tests use throwaway
databases below `/tmp` so they never touch `/var/lib/herald/herald.db`.

```bash
# Manual collect (bird → parse → context-enrich → image-enrich → ingest)
python3 herald_digest.py collect

# Manual digest — real LLM call (~90s, ~$0.009 with default model)
python3 herald_digest.py digest --since "12h ago" --top 7 --max-candidates 40

# Dry-run digest — pipeline runs end-to-end without LLM calls (free)
python3 herald_digest.py digest --since "12h ago" --dry-run --show

# Run the two test suites (28 cases total — no live Twitter or OpenRouter needed)
HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_parser_v2.py     # 20 cases
HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_image_context.py #  8 cases

# Smoke-test the mock bird CLI
HERALD_DB_PATH=/tmp/herald-test/test.db ./test_mock_bird --help

# Dev-mode web UI (production runs under gunicorn, see herald-web.service)
python3 herald_web.py   # → 127.0.0.1:8060

# Pipeline health (on optiplex)
ssh optiplex "sqlite3 /var/lib/herald/herald.db \"SELECT step, status, COUNT(*) FROM pipeline_runs WHERE started_at > strftime('%s','now','-1 day') GROUP BY step, status\""
```

## Environment

`herald_env.py` is a **whitelist-based** optional loader. Production values
come from the mutually restricted systemd environment files
`/etc/herald/web.env` and `/etc/herald/worker.env`; values already in the parent
environment win because the loader uses `os.environ.setdefault`. For manual
development, explicitly set `HERALD_ENV_PATH` to a private file outside Git.
The whitelist lives at the top of `herald_env.py` (`LOAD_KEYS`); **add new
`HERALD_*` knobs there** or they will not load from such a file.

Common knobs (full table in README.md):

| Env var | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required for synthesis / embeddings / vision |
| `HERALD_MODEL` | `openai/gpt-oss-120b` | Synthesis LLM (reasoning model) |
| `HERALD_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Pre-filter embeddings |
| `HERALD_VISION_MODEL` | `google/gemini-2.5-flash` | Image captions (Phase 3.6.1) |
| `HERALD_SIM_THRESHOLD` | `0.50` | Cosine threshold for pre-filter cluster |
| `HERALD_MAX_CANDIDATES` | `40` | Pre-filter cap sent to stage 1 |
| `--top` (CLI) | `7` | Stories per digest (Phase 3.7.1: raised from 5) |
| `HERALD_MAX_ITEMS_PER_DIGEST` | `10` | Items (single-post picks) per digest (Phase 3.8) |
| `HERALD_STAGE{1,2}_MAX_TOKENS` | `64000` | Reasoning model needs the room |
| `HERALD_STAGE1_MAX_REASONING_TOKENS` | `4000` | Caps stage 1 chain-of-thought (Phase 3.8.1) |
| `HERALD_STAGE2_MAX_REASONING_TOKENS` | `2000` | Caps stage 2 chain-of-thought (Phase 3.8.1) |
| `HERALD_CONTEXT_MAX_FETCHES` | `30` | Per-collect budget for thread + quote fetches |
| `HERALD_QUOTE_DEPTH` | `5` | Quote-tweet chain levels chased |
| `HERALD_REPLY_PARENT_DEPTH` | `2` | Reply-parent hops per quote target |
| `HERALD_FOLLOW_REPLY_PARENTS` | `1` | Master toggle for reply-chain walker |
| `HERALD_MAX_MEDIA_FETCHES_PER_CYCLE` | `10` | Vision-call budget per collect |
| `HERALD_IMAGES_PER_STORY` | `1` | Per-story cap on image context |
| `HERALD_BIRD_BIN` | `bird` | Override path (use `/home/james/.local/bin/bird-nvm` on optiplex — system `bird` runs on Node 18 and fails) |

## Architecture (one pass)

```
bird home --json-full
  ↓ collect.py            RT resolution + 24h age gate
  ↓ parser.py             normalised JSONL (pure-data, no bird deps)
  ↓ herald_context.enrich_batch         thread/quote fetch (eager, budgeted)
  ↓ herald_media.enrich_media_batch     image-description fetch (vision LLM)
  ↓ herald_ingest.py      write to SQLite (WAL mode)

── digest time (06:00 / 14:00 AEST) ──
  herald_digest.py digest --since ...
    ↓ herald_db.get_posts_in_window
    ↓ herald_embed.select_candidates    embed → cosine cluster → rank → ~40 reps
    ↓ herald_context.attach_context_to_candidates    DB lookup → _thread_context / _quote_context
    ↓ herald_media.attach_media_context  DB lookup → _image_description
    ↓ herald_links.attach_link_context   DB lookup → _link_context
    ↓ herald_synthesise.stage1_timeline_map   editor's editorial survey — outputs
                                              zeitgeist (themes) + stories_to_write +
                                              items (single-post picks) + editor_notes
                                              (Phase 3.8: editor discretion over scope)
    ↓ herald_synthesise.stage2_story          one call per story OR item (Phase 3.8.3:
                                              items become regular stories rows; both
                                              rendered inline). Sliding-scale prompt
                                              adapts depth to n_sources.
    ↓ herald_db.upsert_digest_with_stories    persist (Phase 3.8.3: items = regular rows)
  Web UI (herald_web.py, Flask + gunicorn behind nginx)
```

### Module roles

| File | Role |
|---|---|
| `herald_digest.py` | Unified CLI — `collect` and `digest` subcommands. Orchestrates the whole pipeline. |
| `parser.py` | Pure-data bird tweet → Herald post dict. No bird deps. Three fixes vs prior iteration (media URLs, parent_tweet stub, note_tweet). |
| `herald_links.py` | Phase 3.6.2: HTTP GET + stdlib `HTMLParser` extracts article body for outbound links, cached in `link_context`. No LLM in the middle. Landmark/skip-class detection scopes extraction to article content (handles AP News, similar sites). |
| `collect.py` | RT resolution (re-fetches originals via `bird read`) + 24h age gate. Only file that shells out to bird for RTs. |
| `herald_ingest.py` | JSONL → SQLite upsert (idempotent on `(source, external_id)`). |
| `herald_db.py` | Connection helper (WAL mode, `journal_mode = WAL`), schema init, upserts, `pipeline_runs` logging, query helpers. |
| `herald_context.py` | Thread + quote context cache + lookup. Eager fetcher runs in `collect`; digest-time lookup attaches `_thread_context` / `_quote_context` to candidates. |
| `herald_media.py` | Image-description cache (mirrors `herald_context.py` shape). Videos and animated GIFs are filtered out at extract time. |
| `herald_embed.py` | Embedding API call + greedy cosine cluster + cluster scoring (`(1 + log2(size)) × engagement × log10(followers+10)`). |
| `herald_synthesise.py` | Stage 1 (`stage1_timeline_map`) + stage 2 (`stage2_story`) OpenRouter calls. Renders CONTEXT POSTS, IMAGE CONTEXT blocks. |
| `herald_web.py` | Flask UI. Routes: `/`, `/digest/<id>`, `/debug`. No auth. |
| `herald_env.py` | `.env` loader (whitelist-only, `setdefault`). |

### Key tables (`schema.sql`)

`posts` (source-agnostic: `source` defaults to `'x'`, `post_type ∈ {original, reply, quote, repost}`), `accounts`, `stories`, `story_posts`, `digests` (with `zeitgeist` / `timeline_map` JSON columns for stage-1 metadata; `debate_lines` + `signal_vs_noise` columns retained for back-compat with pre-3.7.1 digests), `pipeline_runs` (every step logged with token/cost tracking), `conversation_context` + `quote_context` (parser-v2 caches), `image_context` (Phase 3.6.1), `link_context` (Phase 3.6.2).

## Source-grounding (hard invariant)

The LLM is constrained in its system prompt to:

1. Cite every factual claim inline as `[post:ID]` (or `[post:rss:<guid>]` for future sources).
2. Embed direct quotes verbatim with `"..." — @author` attribution.
3. Paraphrase, never fabricate. If the source posts don't establish something, the story doesn't say it.

Validation is in the stage-2 prompt itself (no separate validator). Failures result in the `story` field being the raw content with a `_parse_warning` marker; the digest row is still written so debugging is possible. **Do not relax this constraint** — it's the primary reason Herald isn't a hallucination machine.

## Operational gotchas

- **`bird` needs Node ≥ 22.** On optiplex the system Node is 18; the project uses `/home/james/.local/bin/bird-nvm` (Node 26 via nvm). Set `HERALD_BIRD_BIN` to the nvm wrapper, otherwise `bird home` fails with "Unexpected token with".
- **DB is SQLite WAL** (`PRAGMA journal_mode = WAL`). Concurrent connections no longer block. Don't run `herald_digest.py collect` and `herald_digest.py digest` at the same time by accident — interleaved log lines are confusing but not corrupting.
- **Failed fetches are sticky.** `fetch_status='failed'` rows in `conversation_context` / `quote_context` / `image_context` / `link_context` are not retried automatically. Force a retry with `DELETE FROM <table> WHERE fetch_status='failed'` for the relevant id. This is intentional — the upstream failing once usually means it will fail again soon.
- **The default synthesis model `gpt-oss-120b` is a reasoning model.** Reasoning tokens count toward `completion_tokens`. The 64000 `HERALD_STAGE{1,2}_MAX_TOKENS` budget gives the model room. Without a reasoning cap, the model can burn the entire output budget on chain-of-thought and emit partial JSON (`finish_reason=length`, $0.025 of cost for nothing — happened on digest 32, fixed in Phase 3.8.1). Defaults: stage 1 caps reasoning at `HERALD_STAGE1_MAX_REASONING_TOKENS=4000`, stage 2 at `HERALD_STAGE2_MAX_REASONING_TOKENS=2000`. If you see `finish_reason=error` in logs the upstream provider rejected the request — transient, next scheduled run will retry. There is **no model fallback chain** — a single provider outage means the scheduled digest slot returns empty until the next cycle succeeds.
- **`image_context` rows are never auto-evicted.** pbs.twimg.com URLs are content-addressable so the description is stable forever. Re-fetching happens only if you manually `DELETE` the row (e.g. after upgrading the vision model).
- **`herald_media._extract_photo_urls` accepts both `media` (parser shape) and `media_urls` (DB shape)** — don't break this when refactoring.
- **`herald_links._extract_link_urls` accepts both `links` (parser shape) and `link_urls` (DB shape)** — same dual-key contract as `_extract_photo_urls`.
- **`MainContentParser` extraction has two failure modes worth knowing:** (1) if no landmark (`<main>`, `<article>`, role="main", or article-body class) is found, it falls back to "extract everything except SKIP_TAGS" which works for simple blogs but not for bloat-heavy news sites; (2) the `SKIP_CLASS_PATTERNS` list is hand-curated from real sites (currently AP News, others TBD) — expect to add more patterns as new sites appear. Framework is general; specific patterns are not.
- **Ranker is age-adjusted (Phase 3.7.3).** `cluster_score` divides engagement by hours-since-posted (`age_hours = max(0.5, (now - created_at) / 3600)`). A 1h-old rising tweet with 50 likes outranks a 24h-old tweet with 500 likes. Without this, the ranker systematically rewarded plateau. The metric is *not* passed to stage 1 — the editor does editorial work, not math. `cluster_score` accepts a `now_ts` parameter for deterministic test scoring.

## Pipeline-run observability

Every step writes a row to `pipeline_runs` with `started_at`, `finished_at`, `step`, `status`, `rows_in`, `rows_out`, and (for LLM steps) `prompt_tokens`, `completion_tokens`, `cost_usd`, `model`. The `/debug` route renders the last 30 runs. The CLI surfaces non-`stop` `finish_reason` values from OpenRouter loudly in stderr — when a digest comes back empty, that's where to look first.

## Out of scope (don't propose these)

- Email delivery, push notifications, watchdog/alerting — by design (DESIGN.md §"No feedback system / No watchdog"). The web UI is the only delivery surface.
- A feedback system — feed is already curated; ranker is hand-tuned with `log(follower_count + 10)` normalisation.
- Per-account weights, snooze UI, interest/noise buttons — culled in DESIGN phase 4.
- Multi-source collection — designed in but deferred to ROADMAP Phase 4. The schema supports it (`posts.source`); new collectors drop into the same table unchanged.
