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
# parent; 5 = chase depth-1 through depth-5. Each extra level costs one bird
# read in the worst case (when bird's --quote-depth couldn't inline). Most
# quote chains stop by 3; 5 leaves headroom for the rare deep case. Bump
# higher if you encounter longer chains in your feed.
HERALD_QUOTE_DEPTH = int(os.environ.get("HERALD_QUOTE_DEPTH", "5"))

# How many reply-parent levels to chase at each quote target. When a quote
# target is itself a reply (has inReplyToStatusId), fetch its parent and
# treat that parent as part of the context chain too. 1 = one parent up;
# 2 = grandparent too. This is what bridges quote chains through "reply
# that quote-tweets" patterns where the next quote is on the OTHER side
# of a reply boundary. Counts as one budget fetch per level.
HERALD_REPLY_PARENT_DEPTH = int(os.environ.get("HERALD_REPLY_PARENT_DEPTH", "2"))

# Master toggle for the reply-parent chase. When False, only the quote
# dimension is walked; reply parents are ignored. Useful for cost control
# or for debugging whether reply-parent context is materially improving
# synthesis quality.
HERALD_FOLLOW_REPLY_PARENTS = os.environ.get("HERALD_FOLLOW_REPLY_PARENTS", "1") not in ("0", "false", "False", "no")

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


def _store_quote_context(conn, quoted_id, status, data=None, error=None, source=None):
    """Upsert a quote_context row for quoted_id.

    `source` records HOW this row came to be. Stored in `fetch_error` (which
    is reused as a generic "provenance" column). Values:
      - None / NULL            — fetched, legacy rows (no source recorded)
      - 'inline-from-bird'     — depth-1 seeded by parser at ingest
      - 'fetched-via-quote-chain' — fetched because something quote-tweeted this
      - 'fetched-via-reply-chain'  — fetched because something replied to this
    The 'failed' status stores the error message instead of the source.
    """
    now = int(time.time())
    payload = json.dumps(data, ensure_ascii=False) if data else "{}"
    if status == "failed":
        # Error message takes precedence over source for failed rows.
        col = error if error is not None else source
    else:
        col = source
    conn.execute(
        """
        INSERT INTO quote_context (
            quoted_tweet_id, fetched_at, fetch_status, fetch_error, data
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(quoted_tweet_id) DO UPDATE SET
            fetched_at   = excluded.fetched_at,
            fetch_status = excluded.fetch_status,
            fetch_error  = CASE WHEN excluded.fetch_status = 'success'
                               THEN excluded.fetch_error ELSE fetch_error END,
            data         = CASE WHEN excluded.fetch_status = 'success'
                               THEN excluded.data ELSE data END
        """,
        (quoted_id, now, status, col, payload),
    )


# ---------- quote-chain expansion ----------

def _fetch_or_load_quote(conn, qid, budget, fetch_kind="fetched-via-quote-chain"):
    """Return (parsed_tweet_dict_or_None, status_str).

    Cache hit (success) -> returns the cached parsed tweet.
    Cache hit (failed)  -> returns None, status='cached-failed'.
    Cache miss          -> fetches via bird read; on success stores + returns,
                           on failure stores 'failed' row + returns None.

    `fetch_kind` records HOW we reached this id so the cache provenance is
    preserved:
      - 'inline-from-bird'      — depth-1 inlined by parser; no fetch
      - 'fetched-via-quote-chain' — fetched because something quote-tweeted this
      - 'fetched-via-reply-chain'  — fetched because something replied to this

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
        _store_quote_context(conn, qid, "success", data=parsed, source=fetch_kind)
        return parsed, "fetched"
    except Exception as e:
        _store_quote_context(conn, qid, "failed", error=str(e)[:200], source=fetch_kind)
        return None, "fetch-failed"


def _expand_context_chain(conn, parsed_dict, quote_depth, reply_depth, visited, budget):
    """Walk both quote and reply dimensions, fetching missing levels.

    Starting from a parsed tweet dict (which may be a stub for the quote
    target of our post, or a fully parsed tweet from a previous fetch), this:

    1. Fetches the tweet if not already cached (single fetch via bird read).
    2. Mutates the dict in place: stub fields replaced with parsed data.
    3. Recurses on the QUOTE dimension (parsed.quoted_tweet) up to
       `quote_depth` levels.
    4. If HERALD_FOLLOW_REPLY_PARENTS is True, also recurses on the REPLY
       dimension (parsed.conversation.parent_id) up to `reply_depth` levels.

    Quote depth only decrements on quote hops; reply depth only decrements on
    reply hops. Either dimension can be saturated independently.

    Cycle-safe via `visited`.

    Returns: (attempted, succeeded, failed, skipped) counts.
      - attempted: actual bird calls made (success or fail)
      - succeeded: bird calls that returned a parseable tweet
      - failed:    bird calls that raised (recorded as 'failed' in cache)
      - skipped:   levels we couldn't reach (cached-failed, budget exhausted)
    """
    attempted = succeeded = failed = skipped = 0

    def _load(qd, fetch_kind):
        """Ensure `qd` (a parsed tweet dict or stub) is fully loaded.

        Returns the parsed dict (or None on failure) and updates counters.
        Mutates `qd` in place: replaces stub fields with parsed data.
        """
        nonlocal attempted, succeeded, failed, skipped
        if not qd or not qd.get("id"):
            return None
        qid = qd.get("id")
        if qid in visited:
            return None
        visited.add(qid)

        parsed, status = _fetch_or_load_quote(conn, qid, budget, fetch_kind=fetch_kind)
        if parsed is None:
            if status == "fetch-failed":
                attempted += 1
                failed += 1
            elif status == "cached-failed":
                pass  # already tried, no recount
            else:
                skipped += 1  # budget exhausted, etc.
            return None

        if status == "fetched":
            attempted += 1
            succeeded += 1
        # 'cached' or 'inline-from-bird' rows count as success without fetch.

        # Replace stub fields with parsed data so the in-memory parsed dict
        # has the full tweet (text, author, metrics, conversation, etc.).
        for k, v in parsed.items():
            qd[k] = v
        return parsed

    def _walk(qd, qd_reply_parent, quote_depth_remaining, reply_depth_remaining):
        """Recurse into qd's quote chain and (optionally) qd_reply_parent.

        qd is the tweet whose quote chain we follow (qd -> qd.quoted_tweet -> ...).
        qd_reply_parent is the reply parent of qd (None if qd is not a reply,
        or if reply_depth_remaining is 0).

        For each new tweet reached via the quote chain, we ALSO load ITS reply
        parent (if any) so the two dimensions stay intertwined. Otherwise a
        quote-of-reply-of-quote pattern would only follow the quote half and
        miss the reply half at deeper levels.

        Symmetrically, when we recurse into qd_reply_parent, we ALSO follow
        THAT's reply chain (load its parent, recurse) — otherwise a pure
        reply chain with N>2 replies would stall after the first hop. The
        `qd_reply_parent` passed in is already loaded; we recursively load
        its parent here.
        """
        # Quote chain (decrements quote_depth only)
        if quote_depth_remaining > 0 and qd:
            inner = qd.get("quoted_tweet")
            if inner and inner.get("id"):
                loaded = _load(inner, "fetched-via-quote-chain")
                if loaded is not None:
                    # Load the reply parent of the NEW quote target too, so
                    # we can recurse into its reply chain from the next level.
                    inner_parent = None
                    if HERALD_FOLLOW_REPLY_PARENTS and reply_depth_remaining > 0:
                        parent_id = (loaded.get("conversation") or {}).get("parent_id")
                        if parent_id:
                            parent_stub = {
                                "id": parent_id, "url": "", "text": None, "author": None,
                            }
                            inner_parent = _load(parent_stub, "fetched-via-reply-chain")
                            if inner_parent is not None:
                                loaded["_parent_tweet"] = inner_parent
                    _walk(loaded, inner_parent, quote_depth_remaining - 1, reply_depth_remaining)

        # Reply chain (decrements reply_depth only). When recursing into
        # qd_reply_parent, also load ITS reply parent (handled by the same
        # branch below) so we can chain N replies deep.
        if HERALD_FOLLOW_REPLY_PARENTS and reply_depth_remaining > 0 and qd_reply_parent is not None:
            # First recurse into qd_reply_parent's quote chain at full depth.
            _walk(qd_reply_parent, None, quote_depth_remaining, reply_depth_remaining - 1)

            # Then load qd_reply_parent's reply parent (if any) for the next
            # hop up the reply chain.
            inner_parent_id = (qd_reply_parent.get("conversation") or {}).get("parent_id")
            if inner_parent_id and reply_depth_remaining - 1 > 0:
                parent_stub = {
                    "id": inner_parent_id, "url": "", "text": None, "author": None,
                }
                inner_parent = _load(parent_stub, "fetched-via-reply-chain")
                if inner_parent is not None:
                    qd_reply_parent["_parent_tweet"] = inner_parent
                    _walk(inner_parent, None, quote_depth_remaining, reply_depth_remaining - 2)

    # First, ensure the starting tweet is loaded.
    starting_parsed = _load(parsed_dict, "fetched-via-quote-chain")
    if starting_parsed is None:
        return attempted, succeeded, failed, skipped

    # Find the reply parent (if any) and load it.
    parent_parsed = None
    if HERALD_FOLLOW_REPLY_PARENTS and reply_depth > 0:
        conv = starting_parsed.get("conversation") or {}
        parent_id = conv.get("parent_id")
        if parent_id and parent_id not in visited:
            # Wrap the parent_id in a stub dict so _load can process it.
            parent_stub = {"id": parent_id, "url": "", "text": None, "author": None}
            parent_loaded = _load(parent_stub, "fetched-via-reply-chain")
            if parent_loaded is not None:
                # Stash on the starting dict so the attach path can find it.
                starting_parsed["_parent_tweet"] = parent_loaded
                parent_parsed = parent_loaded

    # Recurse from the starting tweet, with the loaded reply parent.
    _walk(starting_parsed, parent_parsed, quote_depth - 1, reply_depth)
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

        # 4. Walk context chains (quote + reply dimensions) via
        # _expand_context_chain. The helper handles cache lookups, fetching,
        # and recursion across both dimensions internally.
        for p in posts:
            if p.get("post_type") != "quote":
                continue
            quoted = p.get("quoted_tweet") or {}
            if not quoted.get("id"):
                continue
            try:
                attempted_, succeeded_, failed_, skipped_ = _expand_context_chain(
                    conn, quoted, HERALD_QUOTE_DEPTH, HERALD_REPLY_PARENT_DEPTH,
                    visited_quote_ids, budget,
                )
                attempted += attempted_
                succeeded += succeeded_
                failed += failed_
                skipped += skipped_
            except Exception as e:
                # Defensive: never let quote expansion abort the whole batch.
                # A failure here leaves the stub in place; next cycle retries.
                print(f"[enrich] WARN: context chain walk failed for "
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


def _load_all_reachable(conn, top_ids, follow_reply_parents=True):
    """Load every quote_context row reachable from `top_ids` via the quote
    AND reply dimensions. No depth cap — this is a best-effort pre-load; the
    depth caps are enforced at FETCH time (enrich_batch), not at load time.

    Returns dict: tweet_id -> parsed tweet dict. Cycles broken via the
    visited set.
    """
    result = {}
    if not top_ids:
        return result
    visited = set()
    frontier = set(top_ids)
    while frontier:
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
            if follow_reply_parents:
                conv = parsed.get("conversation") or {}
                parent_id = conv.get("parent_id")
                if parent_id and parent_id not in visited:
                    next_frontier.add(parent_id)
        frontier = next_frontier
    return result


def attach_context_to_candidates(candidates):
    """For each candidate post, look up cached thread/quote context and attach.

    Mutates each candidate in-place:
      candidate['_thread_context'] = [list of parsed tweets in the same conversation]
      candidate['_quote_context']  = list of dicts, each with:
        {
          'tweet': parsed_tweet_dict,
          '_chain_kind': 'quote' | 'reply',
        }
        Ordered by traversal (quote first, then its reply parent, then the
        quote parent's quote target, etc.). Length 1 for inline-only quote
        chains; longer when enrich_batch chased deeper levels in either
        dimension.

    Posts whose conversation_id has no cached context don't get _thread_context
    set; the synthesis stage is robust to that and will just see the candidate
    on its own. Chain walks break at the first level missing from cache.

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

    # BFS through quote_context following both quote and reply dimensions.
    # No depth cap here — depth caps are enforced at fetch time in
    # enrich_batch; this is just a pre-load of everything cached for the
    # chain walker below.
    quote_context = _load_all_reachable(conn, quote_ids, follow_reply_parents=HERALD_FOLLOW_REPLY_PARENTS)

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
            # DFS through quote + reply dimensions, alternating. Each item
            # in the emitted chain list carries a _chain_kind tag so the
            # synthesise layer can render quote-vs-reply distinctly.
            chain = []
            visited = set()

            def _visit(node, kind, qd_left, rd_left):
                """Visit `node`, recursing into its quote chain and reply parent."""
                if qd_left <= 0 and rd_left <= 0:
                    return
                cur_id = node.get("id")
                if not cur_id or cur_id in visited:
                    return
                visited.add(cur_id)
                chain.append({"tweet": node, "_chain_kind": kind})

                # Quote dimension (decrement qd_left only)
                if qd_left > 0:
                    inner = node.get("quoted_tweet")
                    if inner and inner.get("id"):
                        next_node = quote_context.get(inner["id"])
                        if next_node is not None:
                            _visit(next_node, "quote", qd_left - 1, rd_left)

                # Reply dimension (decrement rd_left only)
                if rd_left > 0 and HERALD_FOLLOW_REPLY_PARENTS:
                    conv = node.get("conversation") or {}
                    parent_id = conv.get("parent_id")
                    if parent_id and parent_id in quote_context:
                        _visit(quote_context[parent_id], "reply",
                               qd_left, rd_left - 1)

            starting = quote_context[qid]
            _visit(
                starting, "quote",
                qd_left=HERALD_QUOTE_DEPTH - 1,  # starting is depth-1
                rd_left=HERALD_REPLY_PARENT_DEPTH,
            )
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


def estimate_context_cost(posts, max_quote_depth=None, max_reply_depth=None,
                          follow_reply_parents=None, max_fetches=None):
    """Estimate fetch count and approximate context tokens for a batch.

    Walks each post's quote/reply chain the same way enrich_batch would, but
    WITHOUT actually fetching. Counts:
      - how many bird reads WOULD be made (limited by max_fetches budget)
      - how many context tweets WOULD be attached at digest time

    When the chain is fully cached (e.g. after enrich_batch has run), the
    walker uses the cached data and can project the full chain. When only
    the inline seed is cached, the projection is conservative — the
    walker stops at the first uncached node because it can't predict
    downstream structure.

    Tokens are approximated as ~120 tokens per context tweet.

    Returns: {
      'quote_posts': int,             # candidates with quote chains
      'fetch_attempts': int,          # bird reads that WOULD be attempted
      'context_tweets_attached': int, # total context items after dedup
      'approx_input_tokens': int,     # rough context token estimate
    }
    """
    if max_quote_depth is None:
        max_quote_depth = HERALD_QUOTE_DEPTH
    if max_reply_depth is None:
        max_reply_depth = HERALD_REPLY_PARENT_DEPTH
    if follow_reply_parents is None:
        follow_reply_parents = HERALD_FOLLOW_REPLY_PARENTS
    if max_fetches is None:
        max_fetches = HERALD_CONTEXT_MAX_FETCHES

    conn = herald_db.get_connection()
    try:
        visited = set()
        quote_posts = 0
        fetch_attempts = 0
        context_tweets = set()
        budget_left = max_fetches

        def _load_cached_node(qid):
            """If qid is cached, return the parsed data; else None."""
            row = conn.execute(
                "SELECT data FROM quote_context "
                "WHERE quoted_tweet_id = ? AND fetch_status = 'success'",
                (qid,),
            ).fetchone()
            if row:
                try:
                    return json.loads(row["data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return None

        def _would_fetch_and_load(qid):
            """Decide whether qid would be fetched.

            Returns:
              - the cached data if available (no fetch needed)
              - None if it would need a fetch (and increments fetch_attempts)
            """
            nonlocal fetch_attempts, budget_left
            cached = _load_cached_node(qid)
            if cached is not None:
                return cached
            if budget_left <= 0:
                return None
            fetch_attempts += 1
            budget_left -= 1
            return None

        for p in posts:
            if p.get("post_type") != "quote":
                continue
            quoted = p.get("quoted_tweet") or {}
            if not quoted.get("id"):
                continue
            quote_posts += 1
            # BFS-like walk, recursing through cached or fetched nodes.
            queue = [(quoted, max_quote_depth, max_reply_depth)]
            while queue:
                node, qd_left, rd_left = queue.pop(0)
                if not node or not node.get("id"):
                    continue
                qid = node.get("id")
                if qid in visited:
                    continue
                visited.add(qid)
                context_tweets.add(qid)
                # If node is a stub (no text), try the cache. If not cached,
                # this is a fetch we'd need.
                if not node.get("text"):
                    cached = _would_fetch_and_load(qid)
                    if cached is not None:
                        # Use cached data so we can recurse into it.
                        node = cached
                # Walk quote chain
                if qd_left > 0:
                    inner = node.get("quoted_tweet")
                    if inner and inner.get("id"):
                        queue.append((inner, qd_left - 1, rd_left))
                # Walk reply parent
                if follow_reply_parents and rd_left > 0:
                    conv = node.get("conversation") or {}
                    parent_id = conv.get("parent_id")
                    if parent_id:
                        # If we have cached parent data, use it and recurse.
                        parent_cached = _load_cached_node(parent_id)
                        if parent_cached is not None:
                            queue.append((parent_cached, qd_left, rd_left - 1))
                        else:
                            # Would fetch; we don't know downstream structure.
                            if budget_left > 0:
                                fetch_attempts += 1
                                budget_left -= 1
                                context_tweets.add(parent_id)
    finally:
        conn.close()

    approx_tokens = len(context_tweets) * 120
    return {
        "quote_posts": quote_posts,
        "fetch_attempts": fetch_attempts,
        "context_tweets_attached": len(context_tweets),
        "approx_input_tokens": approx_tokens,
    }


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
