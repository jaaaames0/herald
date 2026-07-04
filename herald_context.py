#!/usr/bin/env python3
"""
herald_context.py — Fetch and cache conversation/quote context for Herald.

Two tables; two lookup paths:

  conversation_context — one row per X `conversationId`. The full parsed
    thread, fetched once via `bird thread <id> --json-full`. Used at digest
    synthesis time to give the LLM a reply tweet's parent context. Many of
    our feed posts can share a single conversation, so one fetch serves many
    candidates.

  quote_context — one row per `quotedTweet.id`. A single parsed tweet,
    fetched via `bird read <id> --json-full` only when bird's --quote-depth
    didn't inline the target. Most quotes are inlined by bird already, so
    quote_context fills the gap for deeply nested quotes or quote-fetch
    recovery.

When fetches happen: EAGER, during `herald_digest.py collect`. The reasoning:
  * Collect runs in batches (every 30min peak, every 2h off-peak). We have
    headroom here; the digest path is time-sensitive and shouldn't have to
    wait on bird calls.
  * Fetching once and caching means duplicates across batches collapse.
  * Per-post failures don't block the batch; they're recorded as 'failed'
    rows so we don't retry them every cycle.

When fetches DON'T happen (skipped):
  * conversation_id already has a row in conversation_context (cached)
  * quotedTweet.id already has a row in quote_context (cached)
  * Per-run budget (HERALD_CONTEXT_MAX_FETCHES) exhausted
  * post_type isn't 'reply' or 'quote' (no context needed)

CLI:
  enrich    Read JSONL of parsed posts from stdin, fetch missing context.
            Writes summaries to stderr; doesn't print to stdout (so it
            composes cleanly into collect's subprocess chain if needed).
"""
import argparse
import json
import os
import subprocess
import sys
import time

import herald_db
import parser as herald_parser  # ../parser.py in the same dir


BIRD_BIN = os.environ.get("HERALD_BIRD_BIN", "bird")

# Bound the per-collect cost. Each fetch is ~1-2s of Twitter + a parse.
# Default 30: home pulls ~75 tweets, ~30-50% are replies, ~half hit unique
# conversations, fits comfortably. Override lower if you see bird throttling.
HERALD_CONTEXT_MAX_FETCHES = int(os.environ.get("HERALD_CONTEXT_MAX_FETCHES", "30"))

# Max levels of quote-tweet recursion. 1 = fetch only the immediate quoted
# parent; 3 = chase depth-1, depth-2, depth-3 (most chains stop by then).
# Each extra level costs one bird read in the worst case (when bird's
# --quote-depth couldn't inline). Set higher if you encounter longer chains.
HERALD_QUOTE_DEPTH = int(os.environ.get("HERALD_QUOTE_DEPTH", "3"))

# Per-fetch timeouts (seconds). Threads paginate so a touch longer.
FETCH_TIMEOUT_THREAD = int(os.environ.get("HERALD_CONTEXT_THREAD_TIMEOUT", "30"))
FETCH_TIMEOUT_READ = int(os.environ.get("HERALD_CONTEXT_READ_TIMEOUT", "20"))


# ---------- bird subprocess helpers ----------

def _run_bird(args, timeout):
    """Run bird with the given args. Returns (returncode, stdout, stderr).

    Returns (127, "", message) if bird isn't on PATH / HERALD_BIRD_BIN.
    Returns (124, "", message) on subprocess timeout.
    """
    cmd = [BIRD_BIN] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"bird binary not found: {BIRD_BIN}"
    except subprocess.TimeoutExpired:
        return 124, "", f"bird timeout after {timeout}s"


def fetch_thread_raw(conv_id_or_url):
    """Run `bird thread <id> --json-full`. Returns a JSON-decoded list of tweet dicts.

    Raises RuntimeError on subprocess/JSON failure. Caller is responsible for
    parsing each tweet with herald_parser.parse_tweet().
    """
    rc, stdout, stderr = _run_bird(
        ["thread", str(conv_id_or_url), "--json-full"],
        timeout=FETCH_TIMEOUT_THREAD,
    )
    if rc != 0:
        raise RuntimeError(f"bird thread failed (rc={rc}): {stderr.strip()[:200] or stdout.strip()[:200]}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bird thread returned non-JSON: {e}") from e
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise RuntimeError("bird thread returned empty/non-list payload")
    return data


def fetch_parent_raw(tweet_id_or_url):
    """Run `bird read <id> --json-full`. Returns one tweet dict."""
    rc, stdout, stderr = _run_bird(
        ["read", str(tweet_id_or_url), "--json-full"],
        timeout=FETCH_TIMEOUT_READ,
    )
    if rc != 0:
        raise RuntimeError(f"bird read failed (rc={rc}): {stderr.strip()[:200] or stdout.strip()[:200]}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bird read returned non-JSON: {e}") from e
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise RuntimeError("bird read returned empty/non-dict payload")
    return data


# ---------- cache writers ----------

def _store_conv_context(conn, conv_id, status, thread=None, error=None, source_tweet_id=None):
    """Upsert a conversation_context row for conv_id."""
    now = int(time.time())
    thread_json = json.dumps(thread, ensure_ascii=False) if status == "success" and thread else "[]"
    tweet_count = len(thread) if (status == "success" and thread) else 0
    conn.execute(
        """
        INSERT INTO conversation_context (
            conversation_id, fetched_at, fetch_status, fetch_error,
            thread_json, tweet_count, source_tweet_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            fetched_at      = excluded.fetched_at,
            fetch_status    = excluded.fetch_status,
            fetch_error     = excluded.fetch_error,
            thread_json     = CASE WHEN excluded.fetch_status = 'success'
                                   THEN excluded.thread_json ELSE thread_json END,
            tweet_count     = CASE WHEN excluded.fetch_status = 'success'
                                   THEN excluded.tweet_count ELSE tweet_count END,
            source_tweet_id = excluded.source_tweet_id
        """,
        (conv_id, now, status, error, thread_json, tweet_count, source_tweet_id),
    )


def _store_quote_context(conn, quoted_id, status, data=None, error=None):
    """Upsert a quote_context row for quoted_id."""
    now = int(time.time())
    payload = json.dumps(data, ensure_ascii=False) if data else "{}"
    conn.execute(
        """
        INSERT INTO quote_context (
            quoted_tweet_id, fetched_at, fetch_status, fetch_error, data
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(quoted_tweet_id) DO UPDATE SET
            fetched_at   = excluded.fetched_at,
            fetch_status = excluded.fetch_status,
            fetch_error  = excluded.fetch_error,
            data         = CASE WHEN excluded.fetch_status = 'success'
                               THEN excluded.data ELSE data END
        """,
        (quoted_id, now, status, error, payload),
    )


# ---------- quote-chain expansion ----------

def _fetch_or_load_quote(conn, qid, budget):
    """Return (parsed_tweet_dict_or_None, status_str).

    Cache hit (success) -> returns the cached parsed tweet.
    Cache hit (failed)  -> returns None, status='cached-failed'.
    Cache miss          -> fetches via bird read; on success stores + returns,
                           on failure stores 'failed' row + returns None.

    `budget` is a single-element list [remaining_fetches]; decremented on
    each actual fetch. If budget is exhausted, returns (None, 'skipped-budget').
    """
    cached = conn.execute(
        "SELECT data, fetch_status FROM quote_context WHERE quoted_tweet_id = ?",
        (qid,),
    ).fetchone()
    if cached:
        if cached["fetch_status"] == "success":
            try:
                return json.loads(cached["data"]), "cached"
            except (json.JSONDecodeError, TypeError):
                # Corrupted cache row; fall through to refetch.
                pass
        elif cached["fetch_status"] == "failed":
            return None, "cached-failed"

    if budget[0] <= 0:
        return None, "skipped-budget"
    budget[0] -= 1

    try:
        raw = fetch_parent_raw(qid)
        parsed = herald_parser.parse_tweet(raw)
        _store_quote_context(conn, qid, "success", data=parsed)
        return parsed, "fetched"
    except Exception as e:
        _store_quote_context(conn, qid, "failed", error=str(e)[:200])
        return None, "fetch-failed"


def _expand_quote_chain(conn, quoted_dict, depth_remaining, visited, budget):
    """Walk a quoted_tweet chain in place, fetching deeper levels as needed.

    `quoted_dict` is mutated: stub fields (text=None, author=None) are
    replaced with parsed-tweet data, and the inner quoted_tweet field is
    recursively populated up to `depth_remaining` levels total. Cycle-safe
    via `visited`.

    Returns: (attempted, succeeded, failed, skipped) counts.
      - attempted: actual bird calls made (success or fail)
      - succeeded: bird calls that returned a parseable tweet
      - failed:    bird calls that raised (recorded as 'failed' in cache)
      - skipped:   levels we couldn't reach (cached-failed, budget exhausted)
    """
    attempted = succeeded = failed = skipped = 0

    def _walk(qd, depth):
        nonlocal attempted, succeeded, failed, skipped
        if depth <= 0 or not qd:
            return
        qid = qd.get("id")
        if not qid or qid in visited:
            return
        visited.add(qid)

        parsed, status = _fetch_or_load_quote(conn, qid, budget)
        if parsed is None:
            if status == "fetch-failed":
                attempted += 1
                failed += 1
            elif status == "cached-failed":
                # Already failed in a prior cycle; don't re-count.
                pass
            else:
                # 'skipped-budget' or other terminal states.
                skipped += 1
            return

        if status == "fetched":
            attempted += 1
            succeeded += 1
        # 'cached' or 'inline-from-bird' rows count as success without fetch.

        # Replace stub fields with parsed data so callers (and upsert_post
        # via the in-memory parsed post) see the full tweet.
        for k, v in parsed.items():
            qd[k] = v

        # Recurse into the parsed tweet's own quoted_tweet. If bird's
        # --quote-depth inlined further levels, parsed.quoted_tweet is a
        # full dict and recursion uses that. Otherwise it's a stub (id
        # + url, no text) and recursion will fetch it.
        inner = parsed.get("quoted_tweet")
        if inner and depth > 1:
            _walk(inner, depth - 1)

    _walk(quoted_dict, depth_remaining)
    return attempted, succeeded, failed, skipped


# ---------- entry points ----------

def enrich_batch(posts, max_fetches=None):
    """Fetch missing conversation/quote context for a batch of parsed posts.

    `posts` is a list of post dicts as produced by herald_parser.parse_tweet
    (so each has `post_type`, `conversation.id`, `quoted_tweet.id`).

    For each reply post we haven't fetched yet, run `bird thread <conv_id>
    --json-full` and parse with herald_parser. For each quote post, walk the
    quoted_tweet chain up to HERALD_QUOTE_DEPTH levels; fetch any levels not
    already cached via `bird read <quoted_id> --json-full`. Inline data from
    bird's --quote-depth seeds quote_context for free (the common case needs
    zero fetches). Fetched context is cached in conversation_context /
    quote_context.

    Each successful fetch is recorded; failures are also recorded (status
    'failed' with the error) so we don't retry on every collect cycle.

    Bounded by `max_fetches` (default HERALD_CONTEXT_MAX_FETCHES=30), shared
    across thread fetches and quote-chain fetches.

    Returns: (attempted, succeeded, failed, skipped) — counts for logging.
    """
    if max_fetches is None:
        max_fetches = HERALD_CONTEXT_MAX_FETCHES

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    run_id = herald_db.start_pipeline_run(conn, "context_enrich", source="x", rows_in=len(posts))
    attempted = succeeded = failed = skipped = 0
    started = time.time()

    # Mutable budget shared across both fetch paths. Decremented as we go.
    budget = [max_fetches]

    try:
        # 1. Identify which conversations need fetching (replies only).
        # Dedup within the batch by conversation_id.
        convs_to_fetch = []
        seen_conv = set()
        for p in posts:
            if p.get("post_type") != "reply":
                continue
            conv = p.get("conversation") or {}
            cid = conv.get("id")
            if not cid or cid in seen_conv:
                continue
            seen_conv.add(cid)
            existing = conn.execute(
                "SELECT fetch_status FROM conversation_context WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            if existing:
                # Already cached (success OR failed). Skip; 'failed' means
                # bird has been failing for this conv — give it a few cycles
                # before retrying (re-fetch is cheap if needed; manual run
                # can DELETE the row to force).
                continue
            convs_to_fetch.append((cid, p.get("id")))

        # 2. Walk quote chains. For each quote-type post, recurse into the
        # quoted_tweet field up to HERALD_QUOTE_DEPTH levels. Inline data
        # already in the parsed post is treated as a successful cache hit.
        visited_quote_ids = set()

        # 3. Fetch threads.
        for cid, src_tweet_id in convs_to_fetch:
            if budget[0] <= 0:
                skipped += 1
                continue
            attempted += 1
            budget[0] -= 1
            try:
                raw_tweets = fetch_thread_raw(cid)
                # Parse each; tolerate failures on individual tweets so one
                # weird tweet doesn't blow up the whole thread.
                parsed = []
                for raw in raw_tweets:
                    try:
                        parsed.append(herald_parser.parse_tweet(raw))
                    except Exception:
                        continue
                if not parsed:
                    raise RuntimeError("all thread tweets failed to parse")
                _store_conv_context(
                    conn, cid, "success", thread=parsed, source_tweet_id=src_tweet_id,
                )
                succeeded += 1
            except Exception as e:
                _store_conv_context(
                    conn, cid, "failed", error=str(e)[:200], source_tweet_id=src_tweet_id,
                )
                failed += 1

        # 4. Walk quote chains (depth-first via _expand_quote_chain). The
        # helper handles cache lookups, fetching, and recursion internally.
        for p in posts:
            if p.get("post_type") != "quote":
                continue
            quoted = p.get("quoted_tweet") or {}
            if not quoted.get("id"):
                continue
            try:
                attempted_, succeeded_, failed_, skipped_ = _expand_quote_chain(
                    conn, quoted, HERALD_QUOTE_DEPTH, visited_quote_ids, budget,
                )
                attempted += attempted_
                succeeded += succeeded_
                failed += failed_
                skipped += skipped_
            except Exception as e:
                # Defensive: never let quote expansion abort the whole batch.
                # A failure here leaves the stub in place; next cycle retries.
                print(f"[enrich] WARN: quote chain walk failed for "
                      f"{quoted.get('id')}: {e}", file=sys.stderr)
                failed += 1

        conn.commit()
        elapsed = time.time() - started
        herald_db.finish_pipeline_run(
            conn, run_id, "success",
            rows_out=succeeded,
            error=None if failed == 0 else f"{failed} fetches failed",
        )
        conn.commit()
        return attempted, succeeded, failed, skipped

    except Exception as e:
        conn.rollback()
        herald_db.finish_pipeline_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        conn.close()


def _load_quote_chain(conn, top_ids, max_depth):
    """BFS through quote_context starting from `top_ids`, up to `max_depth` levels.

    Returns dict: quoted_tweet_id -> parsed tweet dict (with nested
    quoted_tweet fields still pointing to other ids in the chain). Cycles
    are broken via the visited set.

    Used by attach_context_to_candidates so the chain walker below has
    every level in memory without a per-level SQL round-trip.
    """
    result = {}
    if not top_ids:
        return result
    frontier = set(top_ids)
    visited = set()
    for _ in range(max_depth):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        next_frontier = set()
        for row in conn.execute(
            f"""
            SELECT quoted_tweet_id, data
            FROM quote_context
            WHERE quoted_tweet_id IN ({placeholders})
              AND fetch_status = 'success'
            """,
            tuple(frontier),
        ):
            qid = row["quoted_tweet_id"]
            if qid in visited:
                continue
            visited.add(qid)
            try:
                parsed = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            result[qid] = parsed
            inner = parsed.get("quoted_tweet")
            if inner and inner.get("id") and inner["id"] not in visited:
                next_frontier.add(inner["id"])
        frontier = next_frontier
    return result


def attach_context_to_candidates(candidates):
    """For each candidate post, look up cached thread/quote context and attach.

    Mutates each candidate in-place:
      candidate['_thread_context'] = [list of parsed tweets in the same conversation]
      candidate['_quote_context']  = [parsed tweet at depth 1, depth 2, ...]
                                     (list, ordered shallowest-to-deepest;
                                     length 1 for inline-only chains, longer
                                     when enrich_batch chased deeper levels)

    Posts whose conversation_id has no cached context don't get _thread_context
    set; the synthesis stage is robust to that and will just see the candidate
    on its own. Quote chains break at the first level missing from cache.

    No-op if candidates is empty or none of them have cached context.
    """
    if not candidates:
        return

    conv_ids = set()
    quote_ids = set()
    for p in candidates:
        conv = p.get("conversation") or {}
        cid = conv.get("id")
        if cid:
            conv_ids.add(cid)
        if p.get("post_type") == "quote":
            q = p.get("quoted_tweet") or {}
            if q.get("id"):
                quote_ids.add(q["id"])

    if not conv_ids and not quote_ids:
        return

    conn = herald_db.get_connection()

    conv_context = {}
    if conv_ids:
        placeholders = ",".join("?" for _ in conv_ids)
        for row in conn.execute(
            f"""
            SELECT conversation_id, thread_json
            FROM conversation_context
            WHERE conversation_id IN ({placeholders})
              AND fetch_status = 'success'
            """,
            tuple(conv_ids),
        ):
            try:
                conv_context[row["conversation_id"]] = json.loads(row["thread_json"])
            except (json.JSONDecodeError, TypeError):
                pass

    # BFS through quote_context to load every level reachable from the
    # candidates' direct quoted tweets. We start with the immediate quote
    # ids on each candidate, then follow each row's nested quoted_tweet
    # references up to HERALD_QUOTE_DEPTH levels. This way the chain walker
    # below has the full depth-N data in memory without a per-level query.
    quote_context = _load_quote_chain(conn, quote_ids, HERALD_QUOTE_DEPTH)

    conn.close()

    for p in candidates:
        conv = p.get("conversation") or {}
        cid = conv.get("id")
        if cid and cid in conv_context:
            p["_thread_context"] = conv_context[cid]
        if p.get("post_type") == "quote":
            q = p.get("quoted_tweet") or {}
            qid = q.get("id")
            if not qid or qid not in quote_context:
                continue
            # Walk the chain shallowest-to-deepest. The parsed depth-N tweet
            # has its own quoted_tweet field (either full or stub). Follow it
            # via the in-memory parsed data so we don't re-query the DB per
            # level. Break when a level isn't cached.
            chain = []
            visited = set()
            current = quote_context[qid]
            depth_cap = HERALD_QUOTE_DEPTH  # hard cap at config depth
            while current and depth_cap > 0:
                cur_id = current.get("id")
                if not cur_id or cur_id in visited:
                    break
                visited.add(cur_id)
                chain.append(current)
                inner = current.get("quoted_tweet")
                if not inner or not inner.get("id"):
                    break
                next_id = inner["id"]
                if next_id in quote_context:
                    current = quote_context[next_id]
                else:
                    # Inner level not cached; the parsed inner dict is a
                    # stub (id+url, no text). Stop here — the LLM still
                    # has everything we *do* have.
                    break
                depth_cap -= 1
            if chain:
                p["_quote_context"] = chain


def get_context_stats(conn=None):
    """Return counts and ages of cached context rows. Useful for the /debug page."""
    own = conn is None
    if own:
        conn = herald_db.get_connection()
    try:
        out = {}
        for label, table in (("conversation", "conversation_context"), ("quote", "quote_context")):
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN fetch_status='success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN fetch_status='failed'  THEN 1 ELSE 0 END) AS failed,
                    MIN(fetched_at) AS oldest_fetch_at,
                    MAX(fetched_at) AS newest_fetch_at
                FROM {table}
                """
            ).fetchone()
            out[label] = dict(row)
        return out
    finally:
        if own:
            conn.close()


# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description="Herald context fetcher")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_enrich = sub.add_parser("enrich", help="Read JSONL of parsed posts from stdin, fetch context")
    p_enrich.add_argument("--max-fetches", type=int, default=None,
                          help="Override HERALD_CONTEXT_MAX_FETCHES for this run")
    p_enrich.set_defaults(func=_cmd_enrich)

    args = p.parse_args()
    sys.exit(args.func(args))


def _cmd_enrich(args):
    """enrich subcommand: read JSONL of parsed posts from stdin, fetch context."""
    posts = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            posts.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"WARN: skipping malformed JSONL line: {e}", file=sys.stderr)

    if not posts:
        print(f"[context] 0 posts on stdin; nothing to do", file=sys.stderr)
        return 0

    attempted, succeeded, failed, skipped = enrich_batch(posts, max_fetches=args.max_fetches)
    print(
        f"[context] in={len(posts)} attempted={attempted} succeeded={succeeded} "
        f"failed={failed} skipped={skipped}",
        file=sys.stderr,
    )
    # Stdout stays empty — this composes cleanly into collect's pipeline if
    # someone wants to wire it in via stdin instead of in-process.
    return 0


if __name__ == "__main__":
    main()
