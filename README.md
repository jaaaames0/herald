# Herald

A distilled read of James's X **Following** feed, twice a day (06:00 and 14:00
Sydney time), in 10–15 minutes, so he doesn't have to open X. The feed shows
through: real posts, verbatim, with what you'd click through to on X (the quoted
post, the post being replied to, what's in the image, the link preview), plus a
thin editor layer. Many accounts reacting to the same specific thing become a
short written story; a run of related posts by one person stays together;
everything else is just the post.

Live at https://herald.jaaaames.com (no auth; single reader, public data).

## How it works

```
hourly     herald.py collect   one `bird home --following` read (200 cards ≈ 3–9h of feed)
                               + ≤25 `bird read`s for missing reply parents / quote levels
                               → SQLite (posts stored whole, with their chains)

06:00      herald.py digest --slot morning     (window 14:00 → 06:00)
14:00      herald.py digest --slot afternoon   (window 06:00 → 14:00)
             1. read every unread image in the window (vision model, cached by URL)
             2. plan  – one call sees EVERY post in the window as a card and picks
                        posts / runs / stories, with priorities
             3. check – a second model verifies each story's posts are about the
                        same specific thing; strays are removed
             4. write – stories written from their posts' full cards; every quote
                        is checked verbatim and every citation against the
                        story's posts; failures are revised once, then cut
             5. trim to ~14 minutes of reading by priority
           → herald_web.py renders posts from storage (never rewritten)
```

Typical cost: ~$0.01–0.05 per digest including vision (see DESIGN.md).

## Files

| File | Role |
|---|---|
| `herald.py` | CLI: `collect`, `ingest FILE…`, `digest --slot/--since`, `show` |
| `herald_x.py` | bird JSON → post dicts (pure; retweets, quote chains, media, cards, ads) |
| `herald_feed.py` | collect, chain completion via `bird read`, `resolve()` |
| `herald_store.py` | SQLite schema and queries (5 tables) |
| `herald_vision.py` | image readings (kind, text in image, description, point) |
| `herald_edit.py` | the editor: window cards, plan/check/write prompts, verbatim checks, trimming |
| `herald_llm.py` | NanoGPT chat client (stdlib) |
| `herald_render_text.py` | plain-text rendering and the reading-time estimate |
| `herald_web.py`, `templates/` | Flask UI: `/`, `/d/<id>`, `/archive`, `/debug` |
| `test_herald.py` | tests (no network) |
| `eval/` | the quality eval: real windows, references, scoring (DESIGN.md §Eval) |
| `deploy/` | systemd units and `release.sh` (build / switch with checkpoint) |

## Run

```bash
python3 -m unittest -v test_herald                                   # tests, no network
python3 herald.py digest --since 2026-10-17T03:00 --until 2026-10-17T19:00 --show
HERALD_DB_PATH=/tmp/x.db python3 herald_web.py                        # dev UI on 127.0.0.1:8060
```

Manual runs read `NANOGPT_API_KEY` (and bird cookies, if collecting) from a
gitignored `.env` next to the code, or from `HERALD_ENV_PATH`.

## Where things live (optiplex)

| What | Where |
|---|---|
| Source | `/home/james/src/herald` |
| Release | `/usr/local/lib/herald/<release>` (root-owned, immutable) |
| Database | `/var/lib/herald/herald-feed.db` (old pipeline: `herald.db`, retained) |
| Logs | `/var/log/herald/{collect,digest,access,error}.log` |
| Config | `/etc/herald/web.env`, `/etc/herald/worker.env` (protected) |
| Units | `herald-web.service`, `herald-collect.{service,timer}`, `herald-digest-{morning,afternoon}.{service,timer}` |
