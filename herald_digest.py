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
from datetime import datetime, timezone
from pathlib import Path

import herald_db
import herald_embed
import herald_env
import herald_synthesise
from herald_env import load_herald_env


HERE = Path(__file__).parent

# Default synthesis model; overridable via HERALD_MODEL in ~/.config/herald/.env
# (which is loaded by load_herald_env() at startup). For the cheap-but-not-free
# baseline we use openai/gpt-oss-120b: ~$0.03/M input, ~$0.15/M output. Reasoning
# model — budget more max_tokens than a non-reasoning model needs.
DEFAULT_MODEL = os.environ.get("HERALD_MODEL", "openai/gpt-oss-120b")

# Pre-filter caps. The pre-filter picks ~max_candidates posts out of the
# window's full set to send to stage 1 (timeline map). Default 40 is plenty
# for a 12h window of X; tweak via CLI flags or env.
DEFAULT_MAX_CANDIDATES = int(os.environ.get("HERALD_MAX_CANDIDATES", "40"))


# ---------- collect subcommand ----------

def cmd_collect(args):
    """Chain: bird home → herald_collect.py → herald_parser.py → herald_ingest.py

    Pipeline order (added 2026-07-03 for parser v2):
      1. bird home (raw feed)
      2. collect.py — RT resolution + age gate
      3. parser.py — normalised JSONL
      4. herald_context.enrich_batch — eager fetch thread/quote context for
         the just-parsed posts, write to conversation_context / quote_context
         so the digest path has warm context. Best-effort; failures don't
         block ingest.
      5. herald_ingest.py — write posts to DB

    Eager (not on-demand) was the design choice for v1: digest time is
    time-sensitive (06:00/14:00 AEST). Collect runs every 30min peak / 2h
    off-peak with headroom for bird calls. Failures are recorded so we
    don't repeatedly retry the same fetch.
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

    # 4. enrich: eager fetch context (best-effort, doesn't block ingest)
    try:
        import herald_context
        parsed_posts = []
        for line in parsed_jsonl.split("\n"):
            line = line.strip()
            if line:
                try:
                    parsed_posts.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[collect] WARN: malformed JSONL line skipped: {e}", file=sys.stderr)
        if parsed_posts:
            attempted, succeeded, failed, skipped = herald_context.enrich_batch(parsed_posts)
            print(
                f"[collect] context: in={len(parsed_posts)} attempted={attempted} "
                f"ok={succeeded} failed={failed} skipped={skipped}",
                file=sys.stderr,
            )
            # Phase 3.6.1: eager fetch of image descriptions for any photo
            # media in this batch. Mirrors the collect-time context pattern;
            # best-effort — failures don't block ingest.
            try:
                import herald_media
                img_attempted, img_ok, img_fail, img_skip = herald_media.enrich_media_batch(parsed_posts)
                if img_attempted or img_fail or img_skip:
                    print(
                        f"[collect] media: attempted={img_attempted} "
                        f"ok={img_ok} failed={img_fail} skipped={img_skip}",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"[collect] WARN: herald_media.enrich_media_batch failed: {e}", file=sys.stderr)
        else:
            print("[collect] context: 0 parsed posts; nothing to enrich", file=sys.stderr)
    except Exception as e:
        print(f"[collect] WARN: herald_context.enrich_batch failed: {e}", file=sys.stderr)

    # 5. ingest (writes to /var/lib/herald/herald.db)
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


def _posts_index_by_id(posts):
    """Index posts by their canonical id (X tweet id) for fast lookup."""
    return {p["id"]: p for p in posts}


def cmd_digest(args):
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

        # 2. Pre-filter: embed + cluster + rank + select
        print(f"[digest] pre-filter: embedding + clustering...", file=sys.stderr)
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
        # synthesis has the full conversational picture.
        n_with_thread = 0
        n_with_quote  = 0
        n_with_image  = 0
        try:
            import herald_context
            herald_context.attach_context_to_candidates(candidates)
            for c in candidates:
                if c.get("_thread_context"):
                    n_with_thread += 1
                if c.get("_quote_context"):
                    n_with_quote += 1
            if n_with_thread or n_with_quote:
                print(
                    f"[digest] context attached: {n_with_thread} threads, {n_with_quote} quote parents",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[digest] WARN: attach_context_to_candidates failed: {e}", file=sys.stderr)

        # Phase 3.6.1: attach image descriptions (lookups into image_context
        # DB; descriptions were fetched earlier during collect). Per-story
        # cap (HERALD_IMAGES_PER_STORY, default 1) is applied later in stage
        # 2 prompt assembly.
        try:
            import herald_media
            herald_media.attach_media_context(candidates)
            for c in candidates:
                if c.get("_image_description"):
                    n_with_image += 1
            if n_with_image:
                print(
                    f"[digest] image context: {n_with_image} candidates with descriptions",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[digest] WARN: herald_media.attach_media_context failed: {e}", file=sys.stderr)

        # 3. Stage 1: timeline map
        print(f"[digest] stage 1: timeline map...", file=sys.stderr)
        timeline_map, s1_usage, s1_model, s1_finish = herald_synthesise.stage1_timeline_map(
            candidates, window_hours=window_hours, dry_run=dry_run,
        )
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
        # Strategy: a theme "owns" a post if the post_id appears in
        # zeitgeist[label].post_ids OR debate_lines[...].post_ids OR
        # signal_vs_noise.noise_patterns[...].post_ids. Falls back to all
        # candidates if we can't map cleanly.
        zeitgeist = (timeline_map or {}).get("zeitgeist") or []
        debate_lines = (timeline_map or {}).get("debate_lines") or []
        s_vs_n = (timeline_map or {}).get("signal_vs_noise") or {}
        noise_pids = set()
        for pat in (s_vs_n.get("noise_patterns") or []):
            if isinstance(pat, dict):
                for pid in pat.get("post_ids") or []:
                    noise_pids.add(pid)
            elif isinstance(pat, str):
                # ignore string descriptions; we need post_ids for mapping
                pass

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
            story, s2_usage, s2_model, s2_finish = herald_synthesise.stage2_story(
                label, theme_posts, angle=angle_str, timeline_map_summary=None, dry_run=dry_run,
            )
            story["post_ids"] = [p["id"] for p in theme_posts]
            story["post_count"] = len(theme_posts)
            # Engagement score for ranking inside the digest
            eng = 0
            for p in theme_posts:
                m = p.get("metrics") or {}
                eng += (m.get("likes") or 0) + 2 * (m.get("retweets") or 0) + (m.get("replies") or 0)
            story["total_score"] = float(eng)
            stories.append(story)
            total_s2_cost += float(s2_usage.get("cost") or 0.0)
            total_s2_in += int(s2_usage.get("prompt_tokens") or 0)
            total_s2_out += int(s2_usage.get("completion_tokens") or 0)

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
            debate_lines=debate_lines,
            signal_vs_noise=s_vs_n,
            timeline_map=timeline_map,
            input_post_ids=input_post_ids,
            window_hours=window_hours,
        )

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
            print(f"  {i}. [{s.get('theme_label','?')}] {s['headline']}{extra}", file=sys.stderr)

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
            for f in ("zeitgeist", "debate_lines", "signal_vs_noise", "input_post_ids"):
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



def main():
    # Load Herald .env first so OPENROUTER_API_KEY / HERALD_MODEL are populated
    # before any synthesis call. Safe to call repeatedly; uses setdefault internally.
    loaded = load_herald_env()
    if loaded:
        print(f"[herald] loaded env from {loaded}", file=sys.stderr)

    parser = argparse.ArgumentParser(description="Herald unified CLI")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_collect = subparsers.add_parser("collect", help="Pull + parse + ingest")
    p_collect.add_argument("-n", type=int, default=75, help="Number of tweets to pull (default: 75)")
    p_collect.set_defaults(func=cmd_collect)

    p_digest = subparsers.add_parser("digest", help="Synthesise a digest from DB posts")
    p_digest.add_argument("--since", default="12h ago", help="Time window, e.g. '12h ago' / '30m ago' / '1d ago' (default: 12h ago)")
    p_digest.add_argument("--top", type=int, default=5, help="Max stories to write in stage 2 (default: 5)")
    p_digest.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, help="Max posts to pre-filter and send to stage 1 LLM (default: 40)")
    p_digest.add_argument("--dry-run", action="store_true", help="Skip LLM calls; use placeholder synthesis (no cost)")
    p_digest.add_argument("--show", action="store_true", help="Print full digest JSON to stdout")
    p_digest.set_defaults(func=cmd_digest)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
