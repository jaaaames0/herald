# Herald

Twice-daily distilled read of the X/Twitter home timeline — and, later, other
sources. Replaces hours of unbounded scrolling with two ~15-minute sit-down
sessions per day. Each digest is a mix of substantive narrative stories and
shorter single-post picks, synthesised by an LLM via OpenRouter, with
inline source citations and hotlinked images.

**The digest is the product.** The reader doesn't click through to X.

## Live at
https://herald.jaaaames.com (TLS via Let's Encrypt, no auth, public)

## Status
- Web UI: live
- Scheduler: live (systemd timers on optiplex, UTC; collect every 2h, digest 06:00 / 14:00 AEST)
- Two-stage synthesis: live (timeline-aware — see DESIGN.md)
- Embedding pre-filter: live (cosine cluster, threshold 0.50)
- First automated digest: 2026-06-28
- Next active roadmap item: **Phase 4 — Multi-source** (see ROADMAP.md)

## How it works (one-liner)

```
bird home -n 75 --following --json-full        # collect
        ↓
  herald_digest.py collect                      # resolve RTs, parse, ingest to SQLite
        ↓
  herald_embed.select_candidates                # embed + cluster, pick ~40 reps
        ↓
  herald_synthesise.stage1_timeline_map         # one LLM call, all candidates
        ↓
  herald_synthesise.stage2_story                # one LLM call per story (≤5)
        ↓
  Web UI at herald.jaaaames.com
```

## Run manually

```bash
cd ~/srv/herald

# Collect + ingest (one-shot)
python3 herald_digest.py collect

# Synthesise a digest for the last 12h (real LLM call)
python3 herald_digest.py digest --since "12h ago" --top 7 --max-candidates 40

# Same, but skip LLM (no cost; useful for testing the pipeline)
python3 herald_digest.py digest --since "12h ago" --dry-run --show
```

## Where things live

| What | Where |
|---|---|
| Code | `/home/james/srv/herald/` (this dir) |
| Database | `/var/lib/herald/herald.db` |
| Logs | `/var/log/herald/{collect,digest,access,error}.log` |
| Auth/env | `/home/james/.config/herald/.env` (chmod 600) |
| Systemd units | `/etc/systemd/system/herald-*.{service,timer}` |
| Nginx vhost | `/etc/nginx/sites-enabled/herald.jaaaames.com` |
| Docs | `README.md`, `DESIGN.md`, `ROADMAP.md`, `CHANGELOG.md` (this dir) |

## Models & tunables (current defaults, override via env)

| Knob | Default | Source |
|---|---|---|
| Synthesis LLM | `openai/gpt-oss-120b` (reasoning model) | `HERALD_MODEL` |
| Stage-1 max tokens | 64000 | `HERALD_STAGE1_MAX_TOKENS` |
| Stage-2 max tokens | 64000 | `HERALD_STAGE2_MAX_TOKENS` |
| Embedding model | `openai/text-embedding-3-small` | `HERALD_EMBEDDING_MODEL` |
| Cluster threshold | 0.50 cosine | `HERALD_SIM_THRESHOLD` |
| Max candidates to LLM | 40 | `HERALD_MAX_CANDIDATES` |
| Stories per digest | 7 | `--top` (Phase 3.7.1: raised from 5) |
| Items per digest | 10 | `HERALD_MAX_ITEMS_PER_DIGEST` (Phase 3.8) |
| Stage-1 reasoning cap | 4000 | `HERALD_STAGE1_MAX_REASONING_TOKENS` (Phase 3.8.1) |
| Stage-2 reasoning cap | 2000 | `HERALD_STAGE2_MAX_REASONING_TOKENS` (Phase 3.8.1) |
| Context max fetches per collect | 30 | `HERALD_CONTEXT_MAX_FETCHES` |
| Thread-fetch timeout (s) | 30 | `HERALD_CONTEXT_THREAD_TIMEOUT` |
| Single-read timeout (s) | 20 | `HERALD_CONTEXT_READ_TIMEOUT` |
| Image context per story | 1 | `HERALD_IMAGES_PER_STORY` |
| Media fetches per collect | 10 | `HERALD_MAX_MEDIA_FETCHES_PER_CYCLE` |
| Vision model | `google/gemini-2.5-flash` | `HERALD_VISION_MODEL` |
| Link context per story | 2 | `HERALD_LINKS_PER_STORY` |
| Link fetches per collect | 10 | `HERALD_MAX_LINK_FETCHES_PER_CYCLE` |
| Link fetch timeout (s) | 10 | `HERALD_LINK_TIMEOUT` |

## Docs in this directory

- **[DESIGN.md](DESIGN.md)** — the design spec. Why Herald exists, architecture, schema, source-grounding requirement, failure modes, decisions.
- **[ROADMAP.md](ROADMAP.md)** — phases, what's done, what's next. The "what are we building and when" doc.
- **[CHANGELOG.md](CHANGELOG.md)** — dated history of changes (newest first).

## Key constraints (full detail in DESIGN.md)

- **Source-grounded synthesis.** Every factual claim in a story cites a source post inline (`[post:ID]`). No bare assertions, no fabrication.
- **Self-contained narrative.** Reader doesn't click through to X — the digest IS the read.
- **No feedback loop.** The feed is already curated; the ranker is hand-tuned with `log(follower_count)` normalisation.
- **No watchdog, no email.** Web UI is the only delivery surface. Debug log + `pipeline_runs` table is the visibility mechanism.

## Parser v2 — context expansion (2026-07-03)

Eager thread/quote-context fetching during `collect`. Replies' parent threads
and quote-tweet targets are fetched once via `bird thread <conv_id>` /
`bird read <quoted_id>`, cached in `conversation_context` / `quote_context`,
and attached to candidates during the digest pass. Stage 1 and Stage 2 prompts
include a separate `CONTEXT POSTS` block so the LLM sees what a reply is
replying to.

```bash
# Run the parser v2 smoke test (uses ./test_mock_bird, doesn't hit Twitter):
HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_parser_v2.py
```