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
  herald_digest.py digest --since "12h ago" --top 7 --show
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from herald_env import load_herald_env

# Load operator configuration before importing modules whose tunables are
# evaluated at import time. This makes manual CLI runs match systemd runs.
load_herald_env()

import herald_db
import herald_embed
import herald_env
import herald_synthesise
import herald_edition
import herald_narrative
import herald_evidence
import herald_desk


HERE = Path(__file__).parent

# Default synthesis model; overridable via HERALD_MODEL in the parent
# environment or the explicitly selected HERALD_ENV_PATH. For the cheap-but-not-free
# baseline we use openai/gpt-oss-120b: ~$0.03/M input, ~$0.15/M output. Reasoning
# model — budget more max_tokens than a non-reasoning model needs.
DEFAULT_MODEL = os.environ.get("HERALD_MODEL", "openai/gpt-oss-120b")

# Pre-filter caps. The pre-filter picks ~max_candidates posts out of the
# window's full set to send to stage 1 (timeline map). Default 40 is plenty
# for a 12h window of X; tweak via CLI flags or env.
DEFAULT_MAX_CANDIDATES = int(os.environ.get("HERALD_MAX_CANDIDATES", "60"))

# Phase 3.8: cap on items (single-post picks from stage 1). Items ride in
# addition to stories, capped independently. The editor already chose the
# count, but this stops a runaway model from flooding the digest.
MAX_ITEMS_PER_DIGEST = int(os.environ.get("HERALD_MAX_ITEMS_PER_DIGEST", "10"))


# ---------- collect subcommand ----------

def cmd_collect(args):
    """Chain: bird home → collect.py → parser.py → herald_ingest.py

    Pipeline order:
      1. bird home (raw feed)
      2. collect.py — preserve observed cards + age gate
      3. parser.py — normalised JSONL
      4. herald_ingest.py — write posts to DB
      5. record ordered snapshot membership.

    Collection makes exactly one X request (``bird home``). Context, media,
    article and retweet-resolution reads are intentionally forbidden here.
    """
    feed_n = args.n

    # 1. bird home (use HERALD_BIRD_BIN if set; falls back to PATH lookup)
    bird_bin = os.environ.get("HERALD_BIRD_BIN", "bird")
    print(f"[collect] {bird_bin} home -n {feed_n} --following --json-full", file=sys.stderr)
    try:
        bird_proc = subprocess.run(
            [bird_bin, "home", "-n", str(feed_n), "--following", "--json-full"],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        print(f"ERROR: bird binary not found: {bird_bin}", file=sys.stderr)
        print("       If using nvm, set HERALD_BIRD_BIN to your nvm-aware wrapper.", file=sys.stderr)
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
    parsed_jsonl = parser.stdout

    parsed_posts = [json.loads(line) for line in parsed_jsonl.splitlines() if line.strip()]

    # 4. ingest (writes to /var/lib/herald/herald.db)
    ingest = subprocess.run(
        ["python3", str(HERE / "herald_ingest.py")],
        input=parsed_jsonl,
        capture_output=True, text=True, timeout=60,
    )
    if ingest.stderr.strip():
        print(ingest.stderr.strip(), file=sys.stderr)
    if ingest.returncode != 0:
        print(f"ERROR: herald_ingest.py failed", file=sys.stderr)
        return 1

    # 5. Persist the order X showed us. This is a DB-only operation.
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        capture_id = herald_db.record_timeline_capture(conn, [p["id"] for p in parsed_posts])
        conn.commit()
        print(f"[collect] snapshot={capture_id} posts={len(parsed_posts)}", file=sys.stderr)
    finally:
        conn.close()

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


def cmd_desk(args):
    """Review accumulated timeline material into persistent, unpublished stories."""
    since_ts, until_ts = parse_window(args.since)
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        if args.replay is not None:
            candidates, usage, model, finish = herald_desk.replay_review(conn, args.replay)
        else:
            candidates, usage, model, finish = herald_desk.review(
                conn, since_ts, until_ts, dry_run=args.dry_run,
            )
        print(
            f"[desk] candidates={len(candidates)} model={model} finish={finish} "
            f"cost=${float(usage.get('cost') or 0):.6f}", file=sys.stderr,
        )
        if args.show:
            print(json.dumps({"candidates": candidates, "usage": usage,
                              "model": model, "finish": finish}, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_desk_evidence(args):
    """Gather bounded source context for ready (or explicitly chosen) desk stories."""
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        results = herald_desk.gather_evidence(
            conn, state=args.state, candidate_ids=args.candidate, dry_run=args.dry_run,
        )
        print(f"[desk-evidence] candidates={len(results)}", file=sys.stderr)
        if args.show:
            print(json.dumps({"candidates": results}, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_desk_status(args):
    """List the accumulated story desk without invoking models or fetchers."""
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        candidates = herald_db.list_story_candidates(conn, state=args.state, limit=args.limit)
        if args.show:
            print(json.dumps({"candidates": candidates}, ensure_ascii=False, indent=2))
        else:
            for candidate in candidates:
                print(
                    f"{candidate['id']:>3}  {candidate['state']:<8} "
                    f"{candidate['readiness']:.2f}  {candidate['source_count']} sources  {candidate['anchor']}"
                )
    finally:
        conn.close()
    return 0


def cmd_desk_packet(args):
    """Print an inspectable, cache-only evidence packet for one desk candidate."""
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        data = herald_desk.packet(conn, args.candidate)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(herald_desk.render_packet_markdown(data), end="")
    finally:
        conn.close()
    return 0


def cmd_desk_write(args):
    """Write an unpublished draft from one desk candidate's cached packet."""
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        if args.replay is not None:
            draft_id = args.replay
            draft = herald_desk.replay_draft(conn, draft_id)
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
            model, finish = draft.get("model_version"), "replay"
        else:
            if args.candidate is None:
                raise ValueError("desk-write requires a candidate unless --replay is used")
            draft_id, draft, usage, model, finish = herald_desk.write_draft(
                conn, args.candidate, dry_run=args.dry_run,
            )
        print(
            f"[desk-write] draft_id={draft_id} model={model} finish={finish} "
            f"cost=${float(usage.get('cost') or 0):.6f}", file=sys.stderr,
        )
        if args.show:
            print(json.dumps(draft, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_desk_issue(args):
    """Create a private, on-demand issue from existing candidate drafts."""
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        issue = herald_desk.create_issue(
            conn, candidate_ids=args.candidate, limit=args.limit, title=args.title,
        )
        print(
            f"[desk-issue] issue_id={issue['id']} items={len(issue['items'])} state=draft cost=$0.000000",
            file=sys.stderr,
        )
        if args.show:
            print(json.dumps(issue, ensure_ascii=False, indent=2))
        else:
            print(herald_desk.render_issue_markdown(issue), end="")
    finally:
        conn.close()
    return 0


def cmd_desk_publish(args):
    """Explicitly publish one private issue; no model or collection work occurs."""
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        issue = herald_desk.publish_issue(conn, args.issue)
        print(
            f"[desk-publish] issue_id={issue['id']} items={len(issue['items'])} state={issue['state']} cost=$0.000000",
            file=sys.stderr,
        )
        if args.show:
            print(json.dumps(issue, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def _posts_index_by_id(posts):
    """Index posts by their canonical id (X tweet id) for fast lookup."""
    return {p["id"]: p for p in posts}


def _extractive_item(post, kind, why):
    """Surface a self-contained short post without inventing a surrounding world."""
    text = (post.get("text") or "").strip()
    author = (post.get("author") or {}).get("username", "unknown")
    return {
        "headline": f"@{author}: {text.replace(chr(10), ' ')[:88]}",
        "story": f"{text} [post:{post['id']}]", "topic": kind, "key_quotes": [],
        "coverage_type": "one_shot", "section": "story", "theme_label": kind, "angle": why,
        "model_version": "extractive-item", "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
    }


def _attach_all_contexts(candidates):
    """Attach cached context (thread/quote/image/link) to candidates.

    Each source is best-effort — a failure in one doesn't abort the others.
    Returns a dict {thread, quote, image, link} of counts for the
    digest-time log line. Centralising the four try/except blocks here
    (rather than 50 lines of repetition in cmd_digest) means future
    context features drop in as a single function call + counter. PR 2
    refactor MR-4.
    """
    counts = {"thread": 0, "quote": 0, "image": 0, "link": 0}

    # Thread + quote context (parser v2, 2026-07-03). DB lookup only —
    # the eager fetch already ran during collect.
    try:
        import herald_context
        herald_context.attach_context_to_candidates(candidates)
        for c in candidates:
            if c.get("_thread_context"):
                counts["thread"] += 1
            if c.get("_quote_context"):
                counts["quote"] += 1
    except Exception as e:
        print(f"[digest] WARN: attach_context_to_candidates failed: {e}", file=sys.stderr)

    # Image descriptions (Phase 3.6.1). Per-story cap
    # (HERALD_IMAGES_PER_STORY) is applied later in stage 2 prompt assembly.
    try:
        import herald_media
        herald_media.attach_media_context(candidates)
        counts["image"] = sum(1 for c in candidates if c.get("_image_description"))
    except Exception as e:
        print(f"[digest] WARN: herald_media.attach_media_context failed: {e}", file=sys.stderr)

    # Link context (Phase 3.6.2). Per-story cap (HERALD_LINKS_PER_STORY)
    # applied later.
    try:
        import herald_links
        herald_links.attach_link_context(candidates)
        counts["link"] = sum(1 for c in candidates if c.get("_link_context"))
    except Exception as e:
        print(f"[digest] WARN: herald_links.attach_link_context failed: {e}", file=sys.stderr)

    return counts


def cmd_edition_digest(args):
    """Generate the bounded, extractive timeline edition."""
    try:
        since_ts, until_ts = parse_window(args.since)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    run_id = herald_db.start_pipeline_run(conn, "edition", source="x")
    try:
        edition = herald_edition.build_edition(conn, since_ts, until_ts, dry_run=args.dry_run)
        if not edition:
            herald_db.finish_pipeline_run(conn, run_id, "skipped", rows_out=0, error="no timeline snapshots in window")
            conn.commit()
            print("[edition] no timeline snapshots in window", file=sys.stderr)
            return 0
        digest_id = herald_db.upsert_digest_with_stories(
            conn, period_start=since_ts, period_end=until_ts, stories=[], sources=["x"],
            model_versions=["edition-editor" if not args.dry_run else "dry-run"],
            input_post_ids=[str(p["external_id"]) for p in edition["cards"]],
            window_hours=(until_ts - since_ts) / 3600.0,
        )
        conn.execute("UPDATE digests SET pipeline_run_id=? WHERE id=?", (run_id, digest_id))
        herald_db.save_edition(conn, digest_id, edition["cards"], edition["clusters"],
                               edition["ledger"], research=edition["research"])
        usage = edition.get("usage") or {}
        herald_db.finish_pipeline_run(
            conn, run_id, "success", rows_out=len(edition["cards"]),
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"), model=herald_edition.EDITOR_MODEL,
        )
        conn.commit()
        print(f"[edition] digest_id={digest_id} shown={edition['ledger']['shown']} "
              f"captured={edition['ledger']['captured']} context_reads={edition['ledger']['context_reads']}", file=sys.stderr)
        if args.show:
            print(json.dumps(herald_db.get_edition(conn, digest_id), ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as e:
        conn.rollback()
        herald_db.finish_pipeline_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        conn.close()


def cmd_narrative_digest(args):
    """Build the bounded Narrative v2 issue from timeline snapshots."""
    try:
        since_ts, until_ts = parse_window(args.since)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    run_id = herald_db.start_pipeline_run(conn, "narrative", source="x")
    # Evidence gathering opens its own cache-writing SQLite connections.
    # Commit the run record before that work so this connection cannot hold
    # the writer lock across selected link/article fetches.
    conn.commit()
    try:
        issue = herald_narrative.build_issue(conn, since_ts, until_ts, dry_run=args.dry_run)
        if not issue:
            herald_db.finish_pipeline_run(conn, run_id, "skipped", rows_out=0, error="no timeline snapshots in window")
            conn.commit()
            return 0
        for story in issue["stories"]:
            story["post_count"] = len(story["post_ids"])
            story["total_score"] = 0.0
            story["cluster_method"] = "narrative-v2"
        digest_id = herald_db.upsert_digest_with_stories(
            conn, period_start=since_ts, period_end=until_ts, stories=issue["stories"], sources=["x"],
            model_versions=[m for m in issue["models"] if m], zeitgeist=None,
            timeline_map={"narrative_v2": True, "plan": issue["plan"], "evidence": issue["evidence"], "finish": issue["finish"]},
            input_post_ids=issue["candidate_ids"],
            window_hours=(until_ts - since_ts) / 3600.0,
        )
        conn.execute("UPDATE digests SET pipeline_run_id=? WHERE id=?", (run_id, digest_id))
        usage = issue["usage"]
        herald_db.finish_pipeline_run(conn, run_id, "success", rows_out=len(issue["stories"]),
                                      prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
                                      cost_usd=usage["cost"], model=issue["models"][-1] if issue["models"] else None)
        conn.commit()
        print(f"[narrative] digest_id={digest_id} stories={len(issue['stories'])} candidates={issue['candidate_count']} "
              f"context_reads={issue['evidence']['context_reads']} finish={issue['finish']}", file=sys.stderr)
        if args.show:
            print(json.dumps(herald_db.get_digest_summary(conn, digest_id), ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as e:
        conn.rollback()
        herald_db.finish_pipeline_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        conn.close()



def cmd_legacy_digest(args):
    """Pull posts in window → pre-filter (embed+cluster) → two-stage LLM digest.

    Pipeline:
      1. Pull posts in window from DB.
      2. Embed all posts (herald_embed); cluster by cosine similarity.
      3. Pick top max_candidates (cluster centroids + 1-2 high-engagement members per cluster).
      4. Stage 1 LLM call: timeline map (themes, debates, signal/noise, stories_to_write).
      5. Stage 2 LLM call per chosen theme: narrative synthesis with citations.
      6. Write digest row + story rows to DB; persist token/cost tracking.

    Dry-run mode skips all LLM calls (no cost) and uses placeholder output.
    """
    dry_run = args.dry_run
    max_stories = args.top
    max_candidates = args.max_candidates

    try:
        since_ts, until_ts = parse_window(args.since)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    window_hours = (until_ts - since_ts) / 3600.0
    print(f"[digest] window: {since_ts} \u2192 {until_ts} ({args.since}, {window_hours:.1f}h)", file=sys.stderr)

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    run_id = herald_db.start_pipeline_run(
        conn, "digest", source="x", rows_in=max_candidates,
    )
    # Targeted evidence opens independent cache-writing connections. Commit
    # this run record before any evidence work so it never holds SQLite's
    # writer lock across link, image, or direct-parent cache writes.
    conn.commit()
    started = time.time()

    try:
        # 1. Pull posts in window
        posts = herald_db.get_posts_in_window(conn, since_ts, until_ts)
        # Rows written before collector-side RT unwrapping are incomplete
        # wrapper cards. Never feed those truncated, misattributed strings to
        # the editor; a fresh collect writes the original under its own ID.
        legacy_retweets = sum(1 for post in posts if post.get("post_type") == "retweet")
        if legacy_retweets:
            posts = [post for post in posts if post.get("post_type") != "retweet"]
            print(f"[digest] excluded {legacy_retweets} legacy retweet wrapper(s)", file=sys.stderr)
        print(f"[digest] {len(posts)} posts in window", file=sys.stderr)
        if not posts:
            herald_db.finish_pipeline_run(conn, run_id, "skipped", rows_out=0)
            conn.commit()
            print("[digest] no posts in window \u2014 nothing to digest", file=sys.stderr)
            return 0

        # 2. Pre-filter: embed + cluster + rank + select
        print(f"[digest] pre-filter: embedding + clustering...", file=sys.stderr)
        if dry_run:
            # A dry run is an offline structural check.  Do not quietly send
            # embeddings to OpenRouter just because selection happens before
            # the editor/writer calls.
            candidates = posts[:max_candidates]
            ranked_clusters = []
            embed_usage = {"prompt_tokens": 0, "cost": 0.0}
        else:
            candidate_idx, ranked_clusters, embed_usage = herald_embed.select_candidates(
                posts, vectors=None, max_candidates=max_candidates,
            )
            candidates = [posts[i] for i in candidate_idx]
        embed_cost = float((embed_usage or {}).get("cost") or 0.0)
        embed_in = int((embed_usage or {}).get("prompt_tokens") or 0)
        n_multi = sum(1 for c in ranked_clusters if len(c["post_indices"]) > 1)
        print(
            f"[digest] candidates: {len(candidates)} (from {len(ranked_clusters)} clusters, "
            f"{n_multi} multi-post); embed cost=${embed_cost:.6f} ({embed_in} input tokens)",
            file=sys.stderr,
        )

        # 2b. Attach cached thread/quote context to candidates (parser v2, 2026-07-03).
        # Fetches happened earlier during collect; this is just a DB lookup.
        # Cached context shows the LLM what a reply is replying to, so reply
        # synthesis has the full conversational picture. All four
        # attachers (thread, quote, image, link) live in one helper; see
        # _attach_all_contexts. PR 2 refactor MR-4.
        ctx_counts = _attach_all_contexts(candidates)
        n_with_thread = ctx_counts["thread"]
        n_with_quote  = ctx_counts["quote"]
        n_with_image  = ctx_counts["image"]
        n_with_link   = ctx_counts["link"]
        if n_with_thread or n_with_quote:
            print(
                f"[digest] context attached: {n_with_thread} threads, {n_with_quote} quote parents",
                file=sys.stderr,
            )
        if n_with_image:
            print(
                f"[digest] image context: {n_with_image} candidates with descriptions",
                file=sys.stderr,
            )
        if n_with_link:
            print(
                f"[digest] link context: {n_with_link} candidates with link extractions",
                file=sys.stderr,
            )

        # 3. Stage 1: timeline map
        print(f"[digest] stage 1: timeline map...", file=sys.stderr)
        timeline_map, s1_usage, s1_model, s1_finish = herald_synthesise.stage1_timeline_map(
            candidates, window_hours=window_hours, dry_run=dry_run,
        )
        # Keep the raw response out of the public timeline-map JSON. It is
        # retained in digest_dossiers for local prompt/quality review.
        stage1_raw = timeline_map.pop("_raw_output", None) if isinstance(timeline_map, dict) else None
        s1_cost = float(s1_usage.get("cost") or 0.0)
        s1_in = int(s1_usage.get("prompt_tokens") or 0)
        s1_out = int(s1_usage.get("completion_tokens") or 0)
        print(
            f"[digest] stage 1 done: cost=${s1_cost:.6f} (in={s1_in} out={s1_out} model={s1_model} finish={s1_finish})",
            file=sys.stderr,
        )
        # Surface budget exhaustion + parse failures loudly so the next silent
        # truncation is impossible to miss in the log.
        if s1_finish == "length":
            print(
                f"[digest] WARNING: stage 1 hit token budget (out={s1_out}); "
                f"consider raising HERALD_STAGE1_MAX_TOKENS.",
                file=sys.stderr,
            )
        elif s1_finish == "error":
            print(
                f"[digest] WARNING: stage 1 finish_reason=error (upstream provider failure). "
                f"Usually transient; will retry on next scheduled run.",
                file=sys.stderr,
            )
        elif s1_finish not in ("stop", "length"):
            print(
                f"[digest] WARNING: stage 1 unexpected finish_reason={s1_finish!r} "
                f"(out={s1_out}).",
                file=sys.stderr,
            )
        if "_parse_warning" in (timeline_map or {}):
            raw = (timeline_map or {}).get("_raw", "")
            finish_in_obj = (timeline_map or {}).get("_finish_reason", s1_finish)
            print(
                f"[digest] WARNING: stage 1 returned non-JSON; timeline map empty for downstream. "
                f"finish={finish_in_obj} raw_preview={raw[:120]!r}",
                file=sys.stderr,
            )

        # Defensive parsing: timeline map may have parse warnings
        stories_to_write = (timeline_map or {}).get("stories_to_write") or []
        if not isinstance(stories_to_write, list):
            stories_to_write = []

        # Build a lookup from theme label to its contributing posts.
        # A theme "owns" a post if the post_id appears in
        # zeitgeist[label].post_ids. Falls back to all candidates if
        # we can't map cleanly. (Phase 3.7.1 dropped debate_lines +
        # signal_vs_noise from the stage-1 output; every post that
        # lands in a zeitgeist theme is fair game.)
        zeitgeist = (timeline_map or {}).get("zeitgeist") or []

        theme_to_posts: dict = {}
        for th in zeitgeist:
            label = th.get("label") if isinstance(th, dict) else None
            if not label:
                continue
            pids = set(th.get("post_ids") or [])
            theme_to_posts[label] = [p for p in candidates if p["id"] in pids]
        # Fallback: any theme with no mapped posts gets all candidates
        # (LLM may have decided to write about the timeline generally).
        for choice in stories_to_write:
            label = choice.get("label") if isinstance(choice, dict) else None
            if not label:
                continue
            if not theme_to_posts.get(label):
                theme_to_posts[label] = list(candidates)

        # 4. Stage 2: per-theme stories
        print(f"[digest] stage 2: writing {min(len(stories_to_write), max_stories)} stories...", file=sys.stderr)
        candidate_ids = {p["id"] for p in candidates}
        pid_to_post = _posts_index_by_id(posts)
        evidence_budget = herald_evidence.new_budget()
        dossier_stories = []
        stories = []
        total_s2_cost = 0.0
        total_s2_in = 0
        total_s2_out = 0
        skipped = 0
        for i, choice in enumerate(stories_to_write[:max_stories]):
            if not isinstance(choice, dict):
                continue
            label = choice.get("label")
            angle = choice.get("angle")
            if not label:
                continue
            theme_posts = theme_to_posts.get(label) or []
            if not theme_posts:
                # If LLM cited posts outside the candidate set, look them up in the full window
                # (catches edge case where the LLM referenced a post we didn't pre-filter in)
                pids = []
                for th in zeitgeist:
                    if isinstance(th, dict) and th.get("label") == label:
                        pids = th.get("post_ids") or []
                        break
                theme_posts = [pid_to_post[p] for p in pids if p in pid_to_post]
            if not theme_posts:
                print(f"[digest]   story {i+1} '{label}' skipped: no posts mapped", file=sys.stderr)
                skipped += 1
                continue
            angle_str = angle if isinstance(angle, str) else None
            evidence = herald_evidence.enrich_story_posts(theme_posts, evidence_budget, dry_run=dry_run)
            story, s2_usage, s2_model, s2_finish = herald_synthesise.stage2_story(
                label, theme_posts, angle=angle_str, timeline_map_summary=None,
                editor_notes=(timeline_map or {}).get("editor_notes") or None,
                dry_run=dry_run,
            )
            story["post_ids"] = [p["id"] for p in theme_posts]
            story["post_count"] = len(theme_posts)
            # Engagement score for ranking inside the digest
            eng = 0
            for p in theme_posts:
                m = p.get("metrics") or {}
                eng += (m.get("likes") or 0) + 2 * (m.get("retweets") or 0) + (m.get("replies") or 0)
            story["total_score"] = float(eng)
            raw_output = story.pop("_raw_output", None)
            quote_rejections = story.pop("_quote_rejections", [])
            citation_rejections = story.pop("_citation_rejections", [])
            dossier_stories.append({
                "label": label, "angle": angle_str, "post_ids": story["post_ids"],
                "evidence": evidence, "writer_raw_output": raw_output,
                "finish_reason": s2_finish, "rejected_key_quotes": quote_rejections,
                "rejected_citations": citation_rejections,
            })
            stories.append(story)
            total_s2_cost += float(s2_usage.get("cost") or 0.0)
            total_s2_in += int(s2_usage.get("prompt_tokens") or 0)
            total_s2_out += int(s2_usage.get("completion_tokens") or 0)

        # Phase 3.8.3: items loop. Each item from stage 1 is a single-post pick
        # that didn't form a theme. Treated as a 1-post cluster; stage 2's
        # sliding-scale prompt produces a short caption or passes the tweet
        # through. Items become regular stories rows — same schema, same UI,
        # same row shape. theme_label carries the item's `kind` so it's
        # queryable but doesn't introduce a new enum.
        items = (timeline_map or {}).get("items") or []
        if not isinstance(items, list):
            items = []
        items = items[:MAX_ITEMS_PER_DIGEST]
        if items:
            print(f"[digest] stage 2: writing {len(items)} items...", file=sys.stderr)
        for j, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_pid = item.get("post_id")
            why = item.get("why") or ""
            kind = item.get("kind") or "item"
            if not item_pid:
                continue
            item_post = pid_to_post.get(item_pid)
            if not item_post:
                # Item cited a post outside the candidate set — skip silently
                # rather than fail the digest. Common case: stage 1 referenced
                # a context post.
                continue
            evidence = herald_evidence.enrich_story_posts([item_post], evidence_budget, dry_run=dry_run)
            needs_writer = (len((item_post.get("text") or "").strip()) > 280 or
                            bool(item_post.get("_quote_context") or item_post.get("_thread_context") or
                                 item_post.get("_link_context") or item_post.get("_image_description")))
            if needs_writer:
                item_story, i2_usage, i2_model, i2_finish = herald_synthesise.stage2_story(
                    f"[{kind}] {item_post.get('text','')[:80]}", [item_post], angle=why,
                    timeline_map_summary=None, editor_notes=(timeline_map or {}).get("editor_notes") or None,
                    dry_run=dry_run,
                )
            else:
                item_story = _extractive_item(item_post, kind, why)
                i2_usage, i2_model, i2_finish = (
                    {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}, "extractive-item", "stop"
                )
            item_story["post_ids"] = [item_post["id"]]
            item_story["post_count"] = 1
            m = item_post.get("metrics") or {}
            item_story["total_score"] = float(
                (m.get("likes") or 0) + 2 * (m.get("retweets") or 0) + (m.get("replies") or 0)
            )
            # theme_label carries the item's kind so it's still a regular row
            # but queryable by kind. No separate `coverage_type` enum — items
            # are first-class stories, just shorter.
            item_story["theme_label"] = kind
            raw_output = item_story.pop("_raw_output", None)
            quote_rejections = item_story.pop("_quote_rejections", [])
            citation_rejections = item_story.pop("_citation_rejections", [])
            dossier_stories.append({
                "label": f"[{kind}] {item_post.get('text','')[:80]}", "angle": why,
                "post_ids": item_story["post_ids"], "evidence": evidence,
                "writer_raw_output": raw_output, "finish_reason": i2_finish,
                "rejected_key_quotes": quote_rejections,
                "rejected_citations": citation_rejections,
            })
            stories.append(item_story)
            total_s2_cost += float(i2_usage.get("cost") or 0.0)
            total_s2_in += int(i2_usage.get("prompt_tokens") or 0)
            total_s2_out += int(i2_usage.get("completion_tokens") or 0)

        print(
            f"[digest] stage 2 done: {len(stories)} stories, "
            f"cost=${total_s2_cost:.6f} (in={total_s2_in} out={total_s2_out})",
            file=sys.stderr,
        )

        # 5. Write digest to DB
        model_versions = sorted({s.get("model_version", "unknown") for s in stories if s.get("model_version")})
        if not model_versions and not dry_run:
            model_versions = [s1_model]
        elif not model_versions:
            model_versions = ["dry-run"]

        input_post_ids = sorted(candidate_ids)
        digest_id = herald_db.upsert_digest_with_stories(
            conn,
            period_start=since_ts,
            period_end=until_ts,
            stories=stories,
            sources=["x"],
            model_versions=model_versions,
            zeitgeist=zeitgeist,
            timeline_map=timeline_map,
            input_post_ids=input_post_ids,
            window_hours=window_hours,
        )
        herald_db.upsert_digest_dossier(conn, digest_id, {
            "pipeline": "legacy-editor-targeted-evidence-v1",
            "stage1": {
                "raw_output": stage1_raw, "finish_reason": s1_finish,
                "usage": s1_usage, "timeline_map": timeline_map,
            },
            "candidate_ids": sorted(candidate_ids),
            "evidence_budget_remaining": evidence_budget,
            "stories": dossier_stories,
        })

        # Aggregate token/cost for the pipeline run (stage1 + stage2 + embed)
        total_in = s1_in + total_s2_in + embed_in
        total_out = s1_out + total_s2_out
        total_cost = s1_cost + total_s2_cost + embed_cost

        herald_db.finish_pipeline_run(
            conn, run_id, "success", rows_out=len(stories),
            prompt_tokens=total_in, completion_tokens=total_out, cost_usd=total_cost,
            model=model_versions[0] if model_versions else None,
        )
        conn.commit()

        elapsed = time.time() - started
        print(f"\n[digest] digest_id={digest_id} stories={len(stories)} elapsed={elapsed:.2f}s", file=sys.stderr)
        if not dry_run:
            print(
                f"[digest] tokens total: in={total_in} out={total_out} cost=${total_cost:.6f} "
                f"(embed=${embed_cost:.6f} stage1=${s1_cost:.6f} stage2=${total_s2_cost:.6f})",
                file=sys.stderr,
            )

        # Print zeigeist + stories summary
        for th in zeitgeist:
            if not isinstance(th, dict):
                continue
            print(
                f"  ZEITGEIST: {th.get('label','?'):20s} | salience={th.get('salience','?'):8s} | "
                f"{len(th.get('accounts') or [])} accounts, {len(th.get('post_ids') or [])} posts",
                file=sys.stderr,
            )
        for i, s in enumerate(stories, 1):
            extra = ""
            if not dry_run:
                extra = f"  [in={s.get('prompt_tokens',0)} out={s.get('completion_tokens',0)} ${s.get('cost_usd',0):.6f}]"
            print(f"  {i}. [{s.get('theme_label','?')}] {s.get('headline') or '<no headline>'}{extra}", file=sys.stderr)

        if skipped:
            print(f"[digest] ({skipped} stories skipped: no posts mapped to theme)", file=sys.stderr)

        # Stdout: full digest as JSON (for piping / debugging)
        if args.show:
            summary = herald_db.get_digest_summary(conn, digest_id)
            for st in summary["stories"]:
                for f in ("key_quotes", "media_references"):
                    if isinstance(st.get(f), str):
                        try:
                            st[f] = json.loads(st[f])
                        except (json.JSONDecodeError, TypeError):
                            st[f] = []
            # Also include the timeline-map metadata fields
            for f in ("zeitgeist", "input_post_ids"):
                v = summary.get(f)
                if isinstance(v, str):
                    try:
                        summary[f] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        pass
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    except Exception as e:
        conn.rollback()
        herald_db.finish_pipeline_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        conn.close()

    return 0


def cmd_digest(args):
    """Build the restored two-stage editorial digest."""
    return cmd_legacy_digest(args)


def main():
    # Load Herald .env first so OPENROUTER_API_KEY / HERALD_MODEL are populated
    # before any synthesis call. Safe to call repeatedly; uses setdefault internally.
    loaded = load_herald_env()
    if loaded:
        print(f"[herald] loaded env from {loaded}", file=sys.stderr)

    parser = argparse.ArgumentParser(description="Herald unified CLI")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_collect = subparsers.add_parser("collect", help="Pull + parse + ingest")
    p_collect.add_argument("-n", type=int, default=100, help="Number of tweets to pull (default: 100)")
    p_collect.set_defaults(func=cmd_collect)

    p_desk = subparsers.add_parser("desk", help="Review accumulated posts into unpublished story candidates")
    p_desk.add_argument("--since", default="72h ago", help="Material to review (default: 72h ago)")
    p_desk.add_argument("--dry-run", action="store_true", help="Persist a deterministic test candidate without calling the editor")
    p_desk.add_argument("--replay", type=int, help="Parse a saved desk review without another model call")
    p_desk.add_argument("--show", action="store_true", help="Print candidate JSON to stdout")
    p_desk.set_defaults(func=cmd_desk)

    p_desk_evidence = subparsers.add_parser("desk-evidence", help="Gather targeted evidence for desk candidates")
    p_desk_evidence.add_argument("--state", default="ready", choices=("watching", "ready"),
                                 help="Candidate state to enrich (default: ready)")
    p_desk_evidence.add_argument("--candidate", type=int, action="append",
                                 help="Specific candidate ID (repeatable; overrides --state)")
    p_desk_evidence.add_argument("--dry-run", action="store_true", help="Report shape without X/article reads")
    p_desk_evidence.add_argument("--show", action="store_true", help="Print evidence audit JSON to stdout")
    p_desk_evidence.set_defaults(func=cmd_desk_evidence)

    p_desk_status = subparsers.add_parser("desk-status", help="List persistent story candidates")
    p_desk_status.add_argument("--state", choices=("watching", "ready", "published"))
    p_desk_status.add_argument("--limit", type=int, default=30)
    p_desk_status.add_argument("--show", action="store_true", help="Print full candidate JSON")
    p_desk_status.set_defaults(func=cmd_desk_status)

    p_desk_packet = subparsers.add_parser("desk-packet", help="Inspect one candidate's cached evidence packet")
    p_desk_packet.add_argument("candidate", type=int)
    p_desk_packet.add_argument("--json", action="store_true", help="Emit structured JSON instead of readable Markdown")
    p_desk_packet.set_defaults(func=cmd_desk_packet)

    p_desk_write = subparsers.add_parser("desk-write", help="Write an unpublished draft from a desk packet")
    p_desk_write.add_argument("candidate", type=int, nargs="?")
    p_desk_write.add_argument("--replay", type=int, help="Reparse a saved draft without another model call")
    p_desk_write.add_argument("--dry-run", action="store_true", help="Create a no-model draft record")
    p_desk_write.add_argument("--show", action="store_true", help="Print draft JSON")
    p_desk_write.set_defaults(func=cmd_desk_write)

    p_desk_issue = subparsers.add_parser("desk-issue", help="Assemble private candidate drafts into an on-demand issue")
    p_desk_issue.add_argument("--candidate", type=int, action="append", help="Candidate ID to include (repeatable)")
    p_desk_issue.add_argument("--limit", type=int, default=6, help="Maximum ready drafts when no candidates are specified")
    p_desk_issue.add_argument("--title", help="Optional private issue title")
    p_desk_issue.add_argument("--show", action="store_true", help="Emit structured private issue JSON")
    p_desk_issue.set_defaults(func=cmd_desk_issue)

    p_desk_publish = subparsers.add_parser("desk-publish", help="Explicitly publish one reviewed issue")
    p_desk_publish.add_argument("issue", type=int)
    p_desk_publish.add_argument("--show", action="store_true", help="Emit published issue JSON")
    p_desk_publish.set_defaults(func=cmd_desk_publish)

    p_digest = subparsers.add_parser("digest", help="Build the two-stage editorial digest")
    p_digest.add_argument("--since", default="12h ago", help="Capture window, e.g. '12h ago' / '30m ago' / '1d ago' (default: 12h ago)")
    p_digest.add_argument("--top", type=int, default=7, help=argparse.SUPPRESS)  # legacy compatibility
    p_digest.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, help=argparse.SUPPRESS)
    p_digest.add_argument("--dry-run", action="store_true", help="Do not call the editor, X context, or web research")
    p_digest.add_argument("--show", action="store_true", help="Print full digest JSON to stdout")
    p_digest.set_defaults(func=cmd_digest)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
