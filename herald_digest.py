#!/usr/bin/env python3
"""
herald_digest.py — Herald's unified CLI.

Subcommands:
  collect     Pull tweets via bird, resolve RTs, parse, ingest to DB.
  digest      Pull posts from DB, cluster, rank, synthesise, write a digest.

Phase 0 mode is dry-run by default for the synthesis step (no LLM call).
Pass --live (when implemented) to call OpenRouter for real.

Examples:
  herald_digest.py collect                       # pull + parse + ingest
  herald_digest.py digest --since "12h ago"      # synthesise (dry-run)
  herald_digest.py digest --since "12h ago" --top 5 --show
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import herald_db


HERE = Path(__file__).parent

# Default synthesis model; overridable via env.
DEFAULT_MODEL = os.environ.get("HERALD_MODEL", "anthropic/claude-3-5-haiku")


# ---------- collect subcommand ----------

def cmd_collect(args):
    """Chain: bird home → herald_collect.py → herald_parser.py → herald_ingest.py"""
    feed_n = args.n

    # 1. bird home
    print(f"[collect] bird home -n {feed_n} --following --json-full", file=sys.stderr)
    try:
        bird_proc = subprocess.run(
            ["bird", "home", "-n", str(feed_n), "--following", "--json-full"],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        print("ERROR: bird not found on PATH. Check ~/.nvm/versions/node/v26.4.0/bin/bird", file=sys.stderr)
        return 2
    if bird_proc.returncode != 0:
        print(f"ERROR: bird home failed: {bird_proc.stderr.strip()[:300]}", file=sys.stderr)
        return 1

    # 2. collector (RT resolution + age gate)
    collector = subprocess.run(
        ["python3", str(HERE / "collect.py")],
        input=bird_proc.stdout,
        capture_output=True, text=True, timeout=120,
    )
    if collector.returncode != 0:
        print(f"ERROR: collect.py failed: {collector.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    if collector.stderr.strip():
        print(collector.stderr.strip(), file=sys.stderr)
    if not collector.stdout.strip():
        print("[collect] no tweets after collector (empty feed?)", file=sys.stderr)
        return 0

    # 3. parser
    parser = subprocess.run(
        ["python3", str(HERE / "parser.py")],
        input=collector.stdout,
        capture_output=True, text=True, timeout=60,
    )
    if parser.returncode != 0:
        print(f"ERROR: parser.py failed: {parser.stderr.strip()[:300]}", file=sys.stderr)
        return 1

    # 4. ingest (writes to /var/lib/herald/herald.db)
    ingest = subprocess.run(
        ["python3", str(HERE / "herald_ingest.py")],
        input=parser.stdout,
        capture_output=True, text=True, timeout=60,
    )
    if ingest.stderr.strip():
        print(ingest.stderr.strip(), file=sys.stderr)
    if ingest.returncode != 0:
        print(f"ERROR: herald_ingest.py failed", file=sys.stderr)
        return 1

    return 0


# ---------- digest subcommand ----------

def parse_window(s):
    """Parse "12h ago" / "30m ago" / "1d ago" → (since_ts, until_ts) unix epoch."""
    s = s.strip()
    m = re.match(r"^(\d+)([smhd])\s*ago$", s)
    if not m:
        raise ValueError(f"--since must be like '12h ago' or '30m ago' or '1d ago', got {s!r}")
    n = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
    until_ts = int(time.time())
    return until_ts - seconds, until_ts


def cluster_posts(posts):
    """Group posts by shared external link (v1 heuristic).

    Posts that share a first link cluster together. Posts with no links
    become singleton clusters. This is intentionally simple; Phase 2 adds
    embedding-based clustering for cross-link topical similarity.
    """
    by_link = defaultdict(list)
    singletons = []

    for p in posts:
        links = p.get("links") or []
        if not links:
            singletons.append(p)
            continue
        by_link[links[0]].append(p)

    clusters = []
    for link, ps in by_link.items():
        clusters.append({"link": link, "posts": ps, "method": "shared_link"})
    for p in singletons:
        clusters.append({"link": None, "posts": [p], "method": "singleton"})
    return clusters


def score_cluster(cluster):
    """Score a cluster by sum of post engagement (likes + 2*retweets + replies)."""
    total = 0.0
    for p in cluster["posts"]:
        m = p.get("metrics") or {}
        likes = m.get("likes", 0) or 0
        rts = m.get("retweets", 0) or 0
        replies = m.get("replies", 0) or 0
        total += likes + 2 * rts + replies
    return total


def synthesise_cluster(cluster, dry_run=True, model=DEFAULT_MODEL):
    """Synthesise a cluster of posts into a story dict.

    In dry_run mode, returns a placeholder so we can validate the pipeline
    structure without spending OpenRouter credits. The real synthesis call
    comes when the OPENROUTER_API_KEY is wired up.
    """
    if dry_run:
        n_posts = len(cluster["posts"])
        link = cluster.get("link")
        n_authors = len({p["author"]["username"] for p in cluster["posts"]})
        if n_posts == 1:
            one = cluster["posts"][0]
            headline = f"[DRY-RUN] {one['author']['username']}: {one['text'][:80]}{'...' if len(one['text']) > 80 else ''}"
        else:
            headline = f"[DRY-RUN] {n_posts} posts from {n_authors} authors on {link[:60] if link else '(no link)'}"
        return {
            "headline": headline,
            "story": f"[DRY-RUN: would synthesise {n_posts} post(s) into a multi-paragraph narrative synthesis with inline [post:ID] citations, embedded quotes, and source-grounding validation. The OpenRouter call is deferred until the API key is wired up.]",
            "topic": "dry-run",
            "key_quotes": [],
            "media_references": [],
            "coverage_type": "one_shot",
            "cluster_method": cluster.get("method", "shared_link"),
            "model_version": "dry-run",
        }

    # Real OpenRouter call (not yet implemented in Phase 0 step 5)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Either set it and re-run, or use --dry-run."
        )
    raise NotImplementedError(
        "Real OpenRouter synthesis call deferred to step 5c. "
        "Use --dry-run to validate the cluster+rank+digest-write pipeline first."
    )


def cmd_digest(args):
    """Pull posts from DB in window, cluster, rank, synthesise, write digest."""
    dry_run = args.dry_run
    top_n = args.top

    try:
        since_ts, until_ts = parse_window(args.since)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"[digest] window: {since_ts} \u2192 {until_ts} ({args.since})", file=sys.stderr)

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    run_id = herald_db.start_pipeline_run(
        conn, "digest", source="x", rows_in=top_n
    )
    started = time.time()

    try:
        # 1. Pull posts in window
        posts = herald_db.get_posts_in_window(conn, since_ts, until_ts)
        print(f"[digest] {len(posts)} posts in window", file=sys.stderr)
        if not posts:
            herald_db.finish_pipeline_run(conn, run_id, "skipped", rows_out=0)
            conn.commit()
            print("[digest] no posts in window \u2014 nothing to digest", file=sys.stderr)
            return 0

        # 2. Cluster
        clusters = cluster_posts(posts)
        multi = sum(1 for c in clusters if len(c["posts"]) > 1)
        print(f"[digest] {len(clusters)} clusters ({multi} multi-post, {len(clusters) - multi} singletons)", file=sys.stderr)

        # 3. Rank
        for c in clusters:
            c["score"] = score_cluster(c)
        clusters.sort(key=lambda c: -c["score"])
        top_clusters = clusters[:top_n]
        top_clusters_summary = ", ".join(
            f"{len(c['posts'])}\u00d7{'(multi)' if len(c['posts']) > 1 else 'singleton'}"
            for c in top_clusters
        )
        print(f"[digest] top {len(top_clusters)} clusters selected: {top_clusters_summary}", file=sys.stderr)

        # 4. Synthesise
        stories = []
        for c in top_clusters:
            s = synthesise_cluster(c, dry_run=dry_run)
            s["post_ids"] = [p["id"] for p in c["posts"]]
            s["post_count"] = len(c["posts"])
            s["total_score"] = c["score"]
            stories.append(s)

        # 5. Write digest to DB
        model_versions = sorted({s.get("model_version", "unknown") for s in stories}) or ["unknown"]
        digest_id = herald_db.upsert_digest_with_stories(
            conn,
            period_start=since_ts,
            period_end=until_ts,
            stories=stories,
            sources=["x"],
            model_versions=model_versions,
        )

        herald_db.finish_pipeline_run(conn, run_id, "success", rows_out=len(stories))
        conn.commit()

        elapsed = time.time() - started
        print(f"\n[digest] digest_id={digest_id} stories={len(stories)} elapsed={elapsed:.2f}s", file=sys.stderr)
        for i, s in enumerate(stories, 1):
            print(f"  {i}. {s['headline']}", file=sys.stderr)

        # Stdout: full digest as JSON (for piping / scripting)
        if args.show:
            summary = herald_db.get_digest_summary(conn, digest_id)
            # Replace the JSON fields with already-deserialised ones
            for st in summary["stories"]:
                for f in ("key_quotes", "media_references"):
                    if isinstance(st.get(f), str):
                        try:
                            st[f] = json.loads(st[f])
                        except (json.JSONDecodeError, TypeError):
                            st[f] = []
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    except Exception as e:
        conn.rollback()
        herald_db.finish_pipeline_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        conn.close()

    return 0


# ---------- entry point ----------

def main():
    parser = argparse.ArgumentParser(description="Herald unified CLI")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_collect = subparsers.add_parser("collect", help="Pull + parse + ingest")
    p_collect.add_argument("-n", type=int, default=75, help="Number of tweets to pull (default: 75)")
    p_collect.set_defaults(func=cmd_collect)

    p_digest = subparsers.add_parser("digest", help="Synthesise a digest from DB posts")
    p_digest.add_argument("--since", default="12h ago", help="Time window, e.g. '12h ago' / '30m ago' / '1d ago' (default: 12h ago)")
    p_digest.add_argument("--top", type=int, default=5, help="Number of top clusters to synthesise (default: 5)")
    p_digest.add_argument("--dry-run", action="store_true", help="Skip LLM call; use placeholder synthesis (default)")
    p_digest.add_argument("--show", action="store_true", help="Print full digest JSON to stdout")
    p_digest.set_defaults(func=cmd_digest)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
