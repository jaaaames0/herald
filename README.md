# Herald

Twice-daily distilled read of the X/Twitter home timeline — and, later, other
sources. Replaces hours of unbounded scrolling with two ~15-minute sit-down
sessions per day. Each digest is a finite, story-based editorial read of the
timeline: what happened, how the people James follows reacted, and the jokes,
arguments, articles, sports, and cultural moments that earned a place.

**The digest is the product.** The reader doesn't click through to X.

## Live at
https://herald.jaaaames.com (TLS via Let's Encrypt, no auth, public)

## Status
- Web UI: live
- Scheduler: intentionally paused pending review
- Timeline snapshots: live in the collector
- Restored editorial pipeline: timeline map → targeted evidence → per-story writer
- Targeted context: ≤10 direct-parent reads per digest; selected quote chains can use that same budget, never during collection
- First automated digest: 2026-06-28
- Next active roadmap item: **Phase 4 — Multi-source** (see ROADMAP.md)

## How it works (one-liner)

```
bird home -n N --following --json-full          # exactly one X request
        ↓
  herald_digest.py collect                      # parse, ingest, preserve ordered snapshot
        ↓
  local embed/cluster + one editor call         # choose themes and timeline items
        ↓
  targeted parent reads + article/image context # only for selected stories
        ↓
  bounded per-story writer calls                # narrative stories with source citations
        ↓
  Web UI at herald.jaaaames.com
```

## Run manually

```bash
cd ~/src/herald

# Collect + ingest (one-shot)
python3 herald_digest.py collect

# Build an editorial digest for the last 12h
python3 herald_digest.py digest --since "12h ago"

# Same, but skip editor, context, and writer calls
python3 herald_digest.py digest --since "12h ago" --dry-run --show
```

## Private review desk

The persistent story desk has a separate, unpublished review interface. It
shows candidate state, private writer drafts, and explicitly assembled
on-demand issues; it does not expose a publish action.

It is disabled by default. To enable it, add a strong, unique value to the
runtime environment file used by `herald-web.service`:

```bash
HERALD_REVIEW_PASSWORD="choose-a-strong-local-secret"
```

Then restart the web service. Visit `/desk` and use HTTP Basic credentials
with username `herald` and that password. Do not put the password in a URL or
commit it to this repository.

Useful desk commands remain local/private:

```bash
python3 herald_digest.py desk-status
python3 herald_digest.py desk-packet 6
python3 herald_digest.py desk-write 6 --show
python3 herald_digest.py desk-issue --candidate 6 --title "Working issue"
```

When the private issue has been reviewed, publication is explicit and costs
nothing (it reuses the saved draft; it does not call a model):

```bash
python3 herald_digest.py desk-publish 1
```

Published issues appear at `/issue/<id>` and the public homepage lists only
published issues. The older `/digest/<id>` records are retained as historical
test data but are no longer linked from the public homepage.

## Where things live

| What | Where |
|---|---|
| Editable source | `/home/james/src/herald/` (this repository) |
| Live runtime | versioned release below `/usr/local/lib/herald/` |
| Database | `/var/lib/herald/herald.db` |
| Logs | `/var/log/herald/{collect,digest,access,error}.log` |
| Web environment | `/etc/herald/web.env` (protected; no worker credentials) |
| Worker environment | `/etc/herald/worker.env` (protected) |
| Systemd units | `/etc/systemd/system/herald-*.{service,timer}` |
| Nginx vhost | `/etc/nginx/sites-enabled/herald.jaaaames.com` |
| Docs | `README.md`, `DESIGN.md`, `ROADMAP.md`, `CHANGELOG.md` (this dir) |

## Models & tunables (current defaults, override via env)

| Knob | Default | Source |
|---|---|---|
| Synthesis LLM | `openai/gpt-oss-120b` (reasoning model) | `HERALD_MODEL` |
| Timeline editor model | inherits synthesis model | `HERALD_EDITOR_MODEL` |
| Story writer model | inherits synthesis model | `HERALD_WRITER_MODEL` |
| Stage-1 max tokens | 6000 (hard max 8000) | `HERALD_STAGE1_MAX_TOKENS` |
| Stage-2 max tokens | 2600 (hard max 4000) | `HERALD_STAGE2_MAX_TOKENS` |
| Embedding model | `openai/text-embedding-3-small` | `HERALD_EMBEDDING_MODEL` |
| Cluster threshold | 0.50 cosine | `HERALD_SIM_THRESHOLD` |
| Max candidates to LLM | 60 | `HERALD_MAX_CANDIDATES` |
| Stories per digest | 7 | `--top` (Phase 3.7.1: raised from 5) |
| Items per digest | 10 | `HERALD_MAX_ITEMS_PER_DIGEST` (Phase 3.8) |
| Stage-1 reasoning effort | `low` | `HERALD_STAGE1_REASONING_EFFORT` |
| Stage-2 reasoning effort | `low` | `HERALD_STAGE2_REASONING_EFFORT` |
| Direct context reads per digest | 10 | `HERALD_EVIDENCE_CONTEXT_READS` |
| Single-read timeout (s) | 20 | `HERALD_CONTEXT_READ_TIMEOUT` |
| Image context per story | 1 | `HERALD_IMAGES_PER_STORY` |
| Image fetches per digest | 4 | `HERALD_EVIDENCE_IMAGE_FETCHES` |
| Vision model | `google/gemini-2.5-flash` | `HERALD_VISION_MODEL` |
| Link context per story | 2 | `HERALD_LINKS_PER_STORY` |
| Link fetches per digest | 8 | `HERALD_EVIDENCE_LINK_FETCHES` |
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

## Targeted context and dossiers

Collection makes one X request, unwraps inline retweets to their original
author/text while retaining amplification provenance, and stores the ordered cards. It logs the
age span of the retained cards: a collection window needs the oldest retained card to extend
past the timer interval, otherwise the chosen page size is clipping the feed. After the editor
selects a story, Herald may read a direct reply parent or a short explicit quote chain,
fetch up to two non-video linked articles, and describe a photo when the post's
text cannot stand alone. It never calls `bird thread` in normal operation.

Every new digest has a local-only dossier containing the Stage 1 response,
evidence operations, and raw Stage 2 responses:

```bash
python3 scripts/digest_dossier.py 55
```
