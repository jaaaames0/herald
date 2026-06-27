# Herald

Twice-daily distilled read of the X/Twitter feed. Replaces hours of unbounded
scrolling with two 15-minute sit-down sessions per day.

Each digest is 5–10 substantive narrative stories, synthesised by an LLM
via OpenRouter, with inline source citations and hotlinked images. The
digest is the product — the reader does not need to click through to X.

## Pipeline

```
bird home -n 75 --following --json-full
        │
        ▼
   herald_collect.py        RT resolution + age gate (drop ads)
        │
        ▼
   herald_parser.py         Pure data transformation, source-agnostic
        │
        ▼
   herald_ingest.py         JSONL → SQLite at /var/lib/herald/herald.db
```

Run manually:

```bash
cd ~/srv/herald
bird home -n 75 --following --json-full | python3 herald_collect.py > unified.json
python3 herald_parser.py < unified.json > parsed.jsonl
python3 herald_ingest.py parsed.jsonl
```

## Layout

- `herald_collect.py` — bird wrapper: resolves RTs via `bird read`, applies 24h age gate
- `herald_parser.py` — pure data transformation; source-agnostic output
- `herald_ingest.py` — JSONL → SQLite upsert
- `herald_db.py` — connection helper, schema init, upsert functions
- `schema.sql` — source of truth for the DB schema
- `validate.py` — end-to-end checks against a parsed.jsonl

## Auth

`herald_collect.py` sources Twitter auth from `~/.config/herald/.env`
(chmod 600). Expected contents:

```
AUTH_TOKEN=***
CT0=***
```

## DB

`/var/lib/herald/herald.db` — single SQLite file. Schema is source-agnostic;
Phase 0 uses only `posts`, `accounts`, `pipeline_runs`. Synthesis tables
(`stories`, `story_posts`, `digests`) are added when we get to that step.

## Design

Full design doc (digital sovereignty repo): `~/docs/project/projects/planned/herald.md`.
