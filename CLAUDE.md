# CLAUDE.md

Guidance for Claude Code in this repository.

## What Herald is

A twice-daily (06:00 / 14:00 Sydney) 10–15 minute read of James's X Following
feed, built so he doesn't open X. Real posts are shown verbatim with their
click-through context; a thin model layer picks, groups and writes short
stories where many accounts react to one specific thing. Read `DESIGN.md`
first (reader, "working" criteria, pipeline, model choices, eval); `README.md`
for files and commands; `ROADMAP.md` (feature freeze); `CHANGELOG.md`.

Lives on `optiplex`, public at https://herald.jaaaames.com, no auth.

## Commands (from `~/src/herald`)

```bash
python3 -m unittest -v test_herald            # all tests; no network, temp DB
python3 herald.py digest --since 2026-10-17T03:00 --until 2026-10-17T19:00 --show
python3 eval/run.py aug19-us mytag --plan deepseek/deepseek-v4.1-flash --write anthropic/claude-sonnet-5.5
python3 eval/score.py aug19-us                # compare variants on a window
HERALD_DB_PATH=/tmp/x.db python3 herald_web.py
```

Manual runs load whitelisted keys (`herald_env.LOAD_KEYS`) from the gitignored
`.env` here or `HERALD_ENV_PATH`; add new `HERALD_*` knobs to that whitelist.
Never print `.env` or `/etc/herald/*.env` contents.

## Production

Never edit `/usr/local/lib/herald/<release>` in place. Commit, then
`deploy/release.sh build` (immutable release + tests inside it) and
`deploy/release.sh switch <release>` (checkpoint + rollback.sh under
`/data/security-backups/`, then units). Follow `~/AGENTS.md`; update
`~/docs/infrastructure/SYSTEM-TOPOLOGY.md` and the operator handbook after an
accepted change. Units run as locked `herald-web` / `herald-worker`.

## Invariants — don't relax

- Post text is rendered from storage, never generated.
- Every quoted span in model prose must pass `herald_edit.quote_ok` against the
  cited material; every paragraph cites; citations must belong to the story.
- A story is one specific thing many accounts react to, verified by the check
  step. Topic-level grouping is the failure mode James named.
- Cost stays around $0.05/digest or less; collection stays at one timeline read
  per hour plus ≤25 bounded reads (the account is cookie-authenticated; don't
  hammer X).

## Gotchas

- `bird` needs Node ≥ 22; production uses `/usr/local/libexec/herald-bird`.
- NanoGPT models occasionally refuse a window ("content_policy_violation") or
  return truncated JSON; `herald_edit._call` retries invalid JSON once and the
  plan once if it is degenerate. Check `/debug` and `digest.log` first.
- Image readings are cached forever by URL (`image_readings`); delete rows to
  re-read after changing the vision model or prompt.
- `herald_x` treats non-retweets older than 48h at capture as ads.
- Out of scope: multi-source, email/push/watchdog, feedback UI, per-account weights.
