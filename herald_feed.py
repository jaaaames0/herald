"""Collect the Following feed and complete the chains a reader would click through.

One `bird home --following` request per collect, then a bounded number of
`bird read` requests for reply parents and quote levels that the timeline
response did not include.
"""

import copy
import json
import os
import subprocess
import time

import herald_store
import herald_x

BIRD_BIN = os.environ.get("HERALD_BIRD_BIN", "bird")
PAGE_SIZE = int(os.environ.get("HERALD_PAGE_SIZE", "200"))
CHAIN_READS = int(os.environ.get("HERALD_CHAIN_READS", "25"))
QUOTE_LEVELS = 4
PARENT_LEVELS = 2


def bird(*args, timeout=180):
    proc = subprocess.run([BIRD_BIN, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"bird {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def ingest_timeline(conn, cards, captured_at=None):
    """Store one timeline response. Returns (posts, new_count)."""
    captured_at = captured_at or int(time.time())
    posts = herald_x.normalise_timeline(cards)
    new = sum(herald_store.upsert_post(conn, p) for p in posts)
    oldest = min((p["seen_at"] for p in posts if p.get("seen_at")), default=None)
    herald_store.record_capture(conn, captured_at, len(cards), len(posts), new, oldest)
    conn.commit()
    return posts, new


def missing_links(conn, post):
    """IDs a reader would click through to that are not stored yet."""
    want = []
    if post.get("reply_to_id"):
        want.append(post["reply_to_id"])
    q, level = post, 0
    while q and level < QUOTE_LEVELS:
        if q.get("quoted_id") and not q.get("quoted"):
            want.append(q["quoted_id"])
            break
        q, level = q.get("quoted"), level + 1
    return [i for i in want if not herald_store.get_post(conn, i)]


def complete_chains(conn, posts, budget=CHAIN_READS, read=None, log=print):
    """Fetch missing reply parents / quote levels, most-engaged posts first."""
    read = read or (lambda pid: herald_x.normalise_read(bird("--quote-depth", "3", "read", str(pid), "--json-full", timeout=60)))
    ranked = sorted(posts, key=lambda p: -(p["metrics"]["likes"] + 3 * len(p.get("retweeted_by") or [])))
    fetched, failed = 0, set()
    for post in ranked:
        for pid in missing_links(conn, post):
            if fetched >= budget:
                return fetched
            if pid in failed:
                continue
            fetched += 1
            try:
                got = read(pid)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                log(f"[chain] read {pid}: {str(e)[:160]}")
                failed.add(pid)
                continue
            if got:
                herald_store.upsert_post(conn, got, context_only=True)
                conn.commit()
    return fetched


def resolve(conn, post, _depth=0):
    """A copy of `post` with its quote chain and reply parents filled from storage."""
    post = copy.deepcopy(post)
    q, level = post, 0
    while q and level < QUOTE_LEVELS:
        if q.get("quoted_id") and not q.get("quoted"):
            q["quoted"] = herald_store.get_post(conn, q["quoted_id"])
        q, level = q.get("quoted"), level + 1
    if post.get("reply_to_id") and _depth < PARENT_LEVELS:
        parent = herald_store.get_post(conn, post["reply_to_id"])
        if parent:
            post["parent"] = resolve(conn, parent, _depth + 1)
    return post


def collect(conn, log=print):
    cards = bird("--quote-depth", "3", "home", "-n", str(PAGE_SIZE), "--following", "--json-full")
    posts, new = ingest_timeline(conn, cards)
    reads = complete_chains(conn, posts, log=log)
    span = (max(p["seen_at"] for p in posts) - min(p["seen_at"] for p in posts)) / 3600 if posts else 0
    log(f"[collect] cards={len(cards)} posts={len(posts)} new={new} chain_reads={reads} span_h={span:.1f}")
    return {"cards": len(cards), "posts": len(posts), "new": new, "chain_reads": reads, "span_h": round(span, 1)}
