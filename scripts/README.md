# Herald dev scripts

Tools for inspecting the digest pipeline state without running the
full LLM loop. Not part of the production runtime; the systemd units
in `/etc/systemd/system/herald-*.{service,timer}` don't reference any
of these. Run by hand when debugging.

## digest_readable.py

Pretty-print a digest as readable text. Shows the zeitgeist list, the
stories with full body, key quotes, and source posts.

```bash
python3 scripts/digest_readable.py [digest_id]
```

If `digest_id` is omitted, prints the most recent digest.

Use when you want a quick read of what a digest actually said without
opening a browser. No LLM call. Read-only against the DB.

## trace_digest.py

End-to-end citation/quote traceability check on a single digest. Walks
the input posts, verifies every `[post:ID]` citation in every story
resolves to a real post, verifies embedded quotes are verbatim
substrings of the source posts, prints a "what was lost" recap (how
many candidates didn't make it into any theme).

```bash
python3 scripts/trace_digest.py [digest_id]
```

Use when investigating a digest that looks off — missing
citations, broken quotes, story drift. No LLM call. Read-only.

## Why these aren't `test_*.py`

The canonical offline test programs are the `test_*.py` files at the project
root. They cover parser/context behavior, media and link extraction, the
restored editorial pipeline, the persistent story desk and its private review
routes. These scripts are interactive — they dump human-readable output to
stdout for inspection.

## History

Moved here 2026-07-10 from the project root as part of the Phase 3.8
sweep. Both scripts predate Herald 1.0; they were originally written
during the newsroom-iteration era to inspect intermediate parser
output. Useful enough to keep, not useful enough to be in the import
path.
