#!/usr/bin/env python3
"""Herald command line.

    herald.py collect                       read the Following feed once, complete chains
    herald.py ingest FILE...                store saved `bird home --json-full` captures
    herald.py digest --slot morning         build the digest for the morning window
    herald.py digest --since 2026-10-17T03:00 --until 2026-10-17T19:00
    herald.py show [ID]                     print a digest as text
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import herald_env

herald_env.load()

import herald_edit  # noqa: E402
import herald_feed  # noqa: E402
import herald_render_text  # noqa: E402
import herald_store  # noqa: E402
import herald_vision  # noqa: E402

TZ = ZoneInfo(os.environ.get("HERALD_TZ", "Australia/Sydney"))
# Each slot covers the feed since the previous slot (local time).
SLOTS = {"morning": (6, 14), "afternoon": (14, 6)}
MODELS = {
    "plan": os.environ.get("HERALD_PLAN_MODEL", "deepseek/deepseek-v4.1-flash"),
    "check": os.environ.get("HERALD_CHECK_MODEL", "anthropic/claude-haiku-5.5"),
    "write": os.environ.get("HERALD_WRITE_MODEL", "anthropic/claude-sonnet-5.5"),
}


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", file=sys.stderr, flush=True)


def slot_window(slot, now=None):
    """(start, end) unix times for the most recent occurrence of a slot."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    end_h, start_h = SLOTS[slot]
    end = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if end > now + timedelta(minutes=30):
        end -= timedelta(days=1)
    start = end.replace(hour=start_h)
    if start >= end:
        start -= timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def parse_when(s):
    d = datetime.fromisoformat(s)
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())


def window_posts(conn, start, end):
    return [herald_feed.resolve(conn, p) for p in herald_store.posts_in_window(conn, start, end)]


def make_digest(conn, start, end, slot, models=MODELS, check=True):
    posts = window_posts(conn, start, end)
    if not posts:
        raise SystemExit(f"no posts between {start} and {end}")
    n, vcost = herald_vision.read_window(conn, posts, log=log)
    log(f"[vision] read {n} images cost=${vcost:.4f}")
    previous = herald_edit.previously(herald_store.previous_digests(conn, start))
    log(f"[digest] {len(previous)} stories from the last editions")
    data = herald_edit.build(herald_edit.Window(posts, start, end, conn), models, log=log, check=check,
                             previous=previous)
    data["cost_usd"] = round(data["cost_usd"] + vcost, 5)
    data["vision"] = {"images_read": n, "cost_usd": round(vcost, 5), "model": herald_vision.MODEL}
    for issue in data["issues"]:
        log(f"[issue] {issue}")
    did = herald_store.save_digest(conn, slot, start, end, models["write"], data["cost_usd"], data)
    conn.commit()
    log(f"[digest] id={did} entries={len(data['entries'])} cost=${data['cost_usd']:.4f}")
    return did, data


def cmd_collect(args):
    conn = herald_store.connect()
    run = herald_store.start_run(conn, "collect")
    try:
        r = herald_feed.collect(conn, log=log)
        herald_store.finish_run(conn, run, "ok", json.dumps(r))
    except Exception as e:
        herald_store.finish_run(conn, run, "failed", str(e)[:500])
        raise


def cmd_ingest(args):
    conn = herald_store.connect()
    for f in args.files:
        cards = json.loads(Path(f).read_text())
        stamp = Path(f).stem.split("-")[-1]
        try:
            captured = int(datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            captured = int(time.time())
        posts, new = herald_feed.ingest_timeline(conn, cards, captured)
        log(f"[ingest] {f}: posts={len(posts)} new={new}")
        if args.chains:
            herald_feed.complete_chains(conn, posts, budget=args.chains, log=log)


def cmd_digest(args):
    conn = herald_store.connect()
    if args.since:
        start, end, slot = parse_when(args.since), parse_when(args.until) if args.until else int(time.time()), "manual"
    else:
        slot = args.slot
        start, end = slot_window(slot)
    run = herald_store.start_run(conn, f"digest-{slot}")
    try:
        did, data = make_digest(conn, start, end, slot)
        herald_store.finish_run(conn, run, "ok", f"digest {did}; {len(data['issues'])} issues", data["cost_usd"])
    except Exception as e:
        herald_store.finish_run(conn, run, "failed", str(e)[:500])
        raise
    if args.show:
        print(herald_render_text.render(data))


def cmd_show(args):
    conn = herald_store.connect()
    d = herald_store.get_digest(conn, args.id)
    if not d:
        raise SystemExit("no digest")
    print(herald_render_text.render(d["data"]))


def main():
    ap = argparse.ArgumentParser(description="Herald: a distilled read of the X Following feed")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect").set_defaults(func=cmd_collect)
    p = sub.add_parser("ingest")
    p.add_argument("files", nargs="+")
    p.add_argument("--chains", type=int, default=0, help="also complete up to N missing chain links via bird read")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("digest")
    p.add_argument("--slot", choices=sorted(SLOTS), default="morning")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--show", action="store_true")
    p.set_defaults(func=cmd_digest)
    p = sub.add_parser("show")
    p.add_argument("id", nargs="?", type=int)
    p.set_defaults(func=cmd_show)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
