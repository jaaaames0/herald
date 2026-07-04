#!/usr/bin/env python3
"""test_parser_v2.py — End-to-end smoke test for parser v2 (context expansion).

What we test:
  1. herald_context.enrich_batch() with a mocked bird subprocess (synthetic
     thread JSON returned via ./test_mock_bird).
  2. herald_db.get_posts_in_window() round-trips the new fields
     (conversation.id, quoted_tweet.id) correctly.
  3. herald_context.attach_context_to_candidates() attaches _thread_context
     to candidates drawn from the seeded DB.
  4. herald_synthesise._render_context_block + _gather_context_for produce
     a sensible CONTEXT POSTS block.
  5. Second-pass enrich_batch is a no-op (cache hits; no re-fetch).
  6. upsert_post round-trips the new conversation/quote fields.
  7. get_posts_in_window re-hydrates the conversation block from DB columns.
  8. Quote-context chain expansion: inline quote is seeded by upsert_post,
     depth-2/3 stubs are fetched by enrich_batch, attach_context walks the
     full chain into _quote_context as a list.
  9. Synthesise prompt renders quote-chain depth with ↳ indentation.
 10. Failed quote fetch is recorded and doesn't abort the batch.
 11-17. Phase 3.5.1: quote-of-reply-of-quote-of-... chain unraveling,
     reply/quote kind markers, toggle-off collapse, cost estimator.

Run from the herald dir:
    HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_parser_v2.py

Requires the companion test_mock_bird executable in the same directory.
The /tmp location keeps the test away from /var/lib/herald/ on chicken
and from optiplex's live DB.
"""
import importlib
import json
import os
import re
import sys
import time as _t
from pathlib import Path

# Test artifacts under /tmp so we don't touch live paths.
TEST_DIR = Path("/tmp/herald-test")
TEST_DIR.mkdir(parents=True, exist_ok=True)
# Use a fresh DB file per test phase so enrich_batch (which opens its own
# SQLite connection internally) doesn't collide with the test's `conn`
# via residual file-handle locks across phase boundaries.
TEST_DB = str(TEST_DIR / "test.db")
os.environ["HERALD_DB_PATH"] = TEST_DB

# Build a mock bird CLI: a single-file executable at /tmp/herald-test/mock_bird
# (created by an earlier setup step). herald_context does
# subprocess.run([HERALD_BIRD_BIN, ...args]), so the env var must point at a
# single executable path, NOT a 'python3 /path/to/script.py' form.
MOCK_BIRD = TEST_DIR / "mock_bird"
if not MOCK_BIRD.exists():
    sys.stderr.write("test setup error: " + str(MOCK_BIRD) + " not found\n")
    sys.exit(2)
os.chmod(MOCK_BIRD, 0o755)
os.environ["HERALD_BIRD_BIN"] = str(MOCK_BIRD)

sys.path.insert(0, "/home/jbruc/optiplex/home/james/srv/herald")

import herald_db
import herald_context
import herald_synthesise
import parser as herald_parser


def banner(msg):
    print("\n=== " + msg + " ===")


def load_test_tweets():
    """test_tweets.json contains TWO concatenated JSON arrays (a hand-edit
    artefact). Load both; return a flat list of parsed tweets."""
    with open("/home/jbruc/optiplex/home/james/srv/herald/test_tweets.json") as f:
        raw = f.read()
    # Strip the trailing comma in the first array's close.
    cleaned = re.sub(r',\s*\](\s*)\[', r']\1[', raw.rstrip())
    # Drop any other trailing-comma-before-] artefacts.
    cleaned = re.sub(r',\s*\](\s*)$', r']\1', cleaned)
    chunks = []
    depth = 0
    start = None
    for i, ch in enumerate(cleaned):
        if ch == '[':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start is not None:
                chunks.append(cleaned[start:i + 1])
                start = None
    if not chunks:
        # Fallback: try single parse.
        return [json.loads(cleaned)]
    out = []
    for c in chunks:
        out.extend(json.loads(c))
    return out


# --- Reset test DB ---
if Path(TEST_DB).exists():
    Path(TEST_DB).unlink()
conn = herald_db.get_connection()
herald_db.init_schema(conn)

banner("Test 1: enrich_batch with mocked bird")

tweets = load_test_tweets()
parsed = [herald_parser.parse_tweet(t) for t in tweets]

print(f"  loaded {len(tweets)} raw tweets, parsed {len(parsed)}")
for i, p in enumerate(parsed):
    parent = p["conversation"]["parent_id"] or "(none)"
    qinfo = ""
    if p.get("quoted_tweet"):
        qinfo = f" quoted={p['quoted_tweet'].get('id')}"
    print(f"    [{i}] id={p['id']} type={p['post_type']} "
          f"conv={p['conversation']['id']} parent={parent}{qinfo}")

attempted, succeeded, failed, skipped = herald_context.enrich_batch(parsed)
print(f"  enrich_batch: attempted={attempted} ok={succeeded} fail={failed} skip={skipped}")
# Conversation fetch: 1 (the unique conv from replies). Quote chain fetches:
# any depth-2/3 levels that weren't inlined.
assert attempted >= 1, f"expected >=1 fetch (the unique conv), got {attempted}"
assert succeeded >= 1, "expected the mock to succeed"
# Quote fetches are optional (depends on test_tweets fixture's quote structure).

rows = conn.execute(
    "SELECT conversation_id, fetch_status, tweet_count, source_tweet_id, thread_json "
    "FROM conversation_context"
).fetchall()
print(f"  conversation_context rows: {len(rows)}")
for r in rows:
    thread = json.loads(r["thread_json"])
    print(f"    conv {r['conversation_id']} status={r['fetch_status']} "
          f"tweets={r['tweet_count']} src={r['source_tweet_id']}")
    for tp in thread:
        print(f"      > thread tweet id={tp['id']} @{tp['author']['username']}")
    assert r["tweet_count"] == 2, "should have 2 thread tweets from mock"

# After enrich_batch, quote_context should be populated for inline quotes.
qrows = conn.execute(
    "SELECT quoted_tweet_id, fetch_status, fetch_error FROM quote_context ORDER BY quoted_tweet_id"
).fetchall()
print(f"  quote_context rows after enrich: {len(qrows)}")
for r in qrows:
    print(f"    quote {r['quoted_tweet_id']} status={r['fetch_status']} "
          f"err={r['fetch_error']}")
# Inline quotes from test_tweets.json should be present (no fetch_error).
assert any(r["fetch_status"] == "success" for r in qrows), \
    "expected at least one successful quote_context row"

banner("Test 2: attach_context_to_candidates")

candidates = []
for p in parsed:
    c = dict(p)
    c["author"] = {"id": p["author"]["id"], "username": p["author"]["username"]}
    candidates.append(c)

herald_context.attach_context_to_candidates(candidates)

for c in candidates:
    qc_info = "absent"
    if c.get("_quote_context") is not None:
        qc = c["_quote_context"]
        if isinstance(qc, list) and qc and isinstance(qc[0], dict) and "tweet" in qc[0]:
            ids = [item["tweet"].get("id") for item in qc]
            kinds = [item.get("_chain_kind") for item in qc]
            qc_info = f"chain len={len(qc)} ids={ids} kinds={kinds}"
        elif isinstance(qc, list):
            ids = [t.get("id") for t in qc]
            qc_info = f"legacy chain len={len(qc)} ids={ids}"
        else:
            qc_info = f"single id={qc.get('id')}"
    print(f"    {c['id'][-10:]} (type={c['post_type']}): "
          f"_thread_context={'present' if '_thread_context' in c else 'absent'} "
          f"_quote_context={qc_info}")

# Every reply should have _thread_context attached
for c in candidates:
    if c["post_type"] == "reply":
        assert "_thread_context" in c, f"reply {c['id']} should have thread context"
        # Should include the OTHER thread tweets (not itself)
        others = [t for t in c["_thread_context"] if t["id"] != c["id"]]
        assert len(others) >= 1, f"reply {c['id']} thread has no others"

# Every quote-tweet candidate should have _quote_context (as a list now)
for c in candidates:
    if c["post_type"] == "quote":
        assert "_quote_context" in c, f"quote {c['id']} missing _quote_context"
        qc = c["_quote_context"]
        assert isinstance(qc, list), f"_quote_context should be a list, got {type(qc)}"
        assert len(qc) >= 1, "chain should have at least depth-1"
        # New format: each item is {"tweet": ..., "_chain_kind": ...}
        for item in qc:
            assert isinstance(item, dict) and "tweet" in item, \
                f"expected new chain-item format, got {item}"
            assert "_chain_kind" in item, f"chain item missing _chain_kind: {item}"

banner("Test 3: prompt rendering (stage 1)")

ctx_posts = herald_synthesise._gather_context_for(candidates)
block, n = herald_synthesise._render_context_block(ctx_posts)
print(f"  _gather_context_for returned {n} context posts")
print(f"  rendered block ({len(block)} chars):")
print("  ---")
for line in block.split("\n")[:25]:
    print(f"  {line[:140]}")
print("  ---")

# Build the user message parts as stage1_timeline_map would
posts_serialised = []
for c in candidates:
    posts_serialised.append({
        "id": c["id"],
        "author": c["author"]["username"],
        "text": (c.get("text") or "")[:200],
    })
user_parts = [
    f"POSTS ({len(posts_serialised)} candidates):\n" + json.dumps(posts_serialised),
]
if block:
    user_parts.append(block)
user_parts.append("Produce the timeline map JSON now.")
combined = "\n\n".join(user_parts)
print(f"  combined stage 1 user message length: {len(combined)} chars")
print(f"  contains 'CONTEXT POSTS': {'CONTEXT POSTS' in combined}")

banner("Test 4: pipeline_runs log")

runs = herald_db.list_pipeline_runs(conn, limit=5, step="context_enrich")
for run in runs:
    print(f"    run #{run['id']} status={run['status']} "
          f"in={run['rows_in']} out={run['rows_out']} err={run['error']}")
assert any(r["status"] == "success" for r in runs), "should have a successful context_enrich run"

banner("Test 5: caching on second pass (no re-fetch)")

# Run enrich_batch again with the same posts; should hit the cache.
attempted2, succeeded2, failed2, skipped2 = herald_context.enrich_batch(parsed)
print(f"  second enrich_batch: attempted={attempted2} ok={succeeded2} fail={failed2} skip={skipped2}")
assert attempted2 == 0, f"second pass should fetch nothing (cache hit), got {attempted2}"
print(f"  ✓ cache hit works (no re-fetch on second pass)")

banner("Test 6: per-post upsert stores the new conversation fields")

# Wipe and re-ingest via upsert_post
herald_db.upsert_post(conn, parsed[1])  # a reply
conn.commit()
row = conn.execute(
    "SELECT conversation_id, quoted_tweet_id, in_reply_to_id FROM posts WHERE external_id = ?",
    (parsed[1]["id"],),
).fetchone()
print(f"  reply {parsed[1]['id'][:10]}... conversation_id={row['conversation_id']} "
      f"parent={row['in_reply_to_id']}")
assert row["conversation_id"] == parsed[1]["conversation"]["id"]
assert row["in_reply_to_id"] == parsed[1]["conversation"]["parent_id"]
print(f"  ✓ round-trip of conversation fields OK")

banner("Test 7: get_posts_in_window re-hydrates the conversation block")

herald_db.upsert_post(conn, parsed[0])
herald_db.upsert_post(conn, parsed[1])
herald_db.upsert_post(conn, parsed[2])
conn.commit()

now = int(__import__("time").time() * 1000)
posts = herald_db.get_posts_in_window(conn, since_ts=0, until_ts=now)
for p in posts:
    print(f"    {p['id'][-6:]}... type={p['post_type']} "
          f"conv={p['conversation']['id'][-6:] if p['conversation']['id'] else 'None'}... "
          f"is_reply={p['conversation']['is_reply']}")
    if p["post_type"] == "reply":
        assert p["conversation"]["is_reply"] is True
        assert p["conversation"]["parent_id"] is not None
print(f"  ✓ get_posts_in_window re-hydrates conversation block OK")

# ---------- Quote chain expansion tests (Phase 3.5 / recursive) ----------

banner("Test 8: inline quote is seeded by upsert_post")

# Re-init test DB so this section is self-contained. Use a fresh DB file to
# avoid SQLite file-handle collisions with prior phase's enrich_batch
# connections.
conn.close()
for stale in TEST_DIR.glob("test-phase*.db"):
    stale.unlink()
TEST_DB = str(TEST_DIR / f"test-phase3-{int(_t.time())}.db")
os.environ["HERALD_DB_PATH"] = TEST_DB
conn = herald_db.get_connection()
herald_db.init_schema(conn)

# Build a synthetic quote-type post with INLINE quoted text (depth-1 inlined
# by parser, simulating bird's --quote-depth 1).
quote_author = {"id": "alice", "username": "alice"}
source_author = {"id": "bob", "username": "bob"}
inline_quote_post = {
    "id": "2000000000000001",
    "url": "https://x.com/alice/status/2000000000000001",
    "text": "look at this",
    "author": quote_author,
    "metrics": {"likes": 1, "retweets": 0, "replies": 0, "quote_count": 0},
    "conversation": {"id": "2000000000000001", "parent_id": None, "is_reply": False, "parent_tweet": None},
    "quoted_tweet": {
        "id": "1000000000000001",  # depth 1 — points at the chain
        "text": "quote-chain depth 1 — points at depth 2",
        "author": {"username": "depth1_author", "name": "Depth 1"},
        "url": "https://x.com/depth1_author/status/1000000000000001",
        "created_at": "Thu Jul 02 23:10:00 +0000 2026",
        "metrics": {"likes": 0, "retweets": 0, "replies": 0, "quote_count": 0},
        # Bird's --quote-depth 1 inlines this level but provides the deeper
        # level as a STUB (id + url, no text). Parser.parse_quoted recurses
        # one level and emits the stub here. herald_context then chases it.
        "quoted_tweet": {
            "id": "1000000000000002",
            "url": "https://x.com/_/status/1000000000000002",
            "text": None,
            "author": None,
        },
    },
    "media": None,
    "links": None,
    "created_at": "Thu Jul 02 23:09:00 +0000 2026",
    "lang": "en",
    "source": "test",
    "post_type": "quote",
}

# Upsert and verify quote_context gets seeded from the inline data.
herald_db.upsert_post(conn, inline_quote_post)
conn.commit()
qc_row = conn.execute(
    "SELECT quoted_tweet_id, fetch_status, fetch_error, data FROM quote_context "
    "WHERE quoted_tweet_id = ?",
    ("1000000000000001",),
).fetchone()
assert qc_row is not None, "upsert_post should have seeded quote_context for the inline quote"
assert qc_row["fetch_status"] == "success"
assert qc_row["fetch_error"] == "inline-from-bird"
print(f"  inline quote seeded: id={qc_row['quoted_tweet_id']} "
      f"status={qc_row['fetch_status']} err={qc_row['fetch_error']}")
print(f"  ✓ inline quote seeding works")

banner("Test 9: enrich_batch chases the quote chain to depth 3")

# enrich_batch on the inline-quote post should:
#   - find depth-1 in cache (inline seed), skip fetch
#   - walk into parsed depth-1's quotedTweet stub (id 1000000000000002)
#   - fetch depth-2 from mock bird
#   - walk into parsed depth-2's quotedTweet stub (id 1000000000000003)
#   - fetch depth-3 from mock bird
#   - depth-3 has no further quotedTweet — chain stops
attempted, succeeded, failed, skipped = herald_context.enrich_batch([inline_quote_post])
print(f"  enrich_batch: attempted={attempted} ok={succeeded} fail={failed} skip={skipped}")
# Expect 2 fetches (depth-2 and depth-3); depth-1 was inline-seeded.
assert attempted == 2, f"expected 2 fetches (depth-2 + depth-3), got {attempted}"
assert succeeded == 2, "both fetches should succeed against the mock"

# Verify all three levels are now cached.
for qid in ("1000000000000001", "1000000000000002", "1000000000000003"):
    r = conn.execute(
        "SELECT fetch_status, fetch_error FROM quote_context WHERE quoted_tweet_id = ?",
        (qid,),
    ).fetchone()
    assert r is not None, f"quote_context row missing for {qid}"
    assert r["fetch_status"] == "success", \
        f"{qid} should be success, got {r['fetch_status']} ({r['fetch_error']})"
    print(f"  ✓ {qid} cached (status={r['fetch_status']}, err={r['fetch_error']})")

banner("Test 10: attach_context walks the full chain into _quote_context list")

# Commit any pending writes; enrich_batch in Test 9 used a separate
# connection, and the test's `conn` may still hold a stale transaction view.
conn.commit()

# Simulate digest-time: pull post from DB (with stub quoted_tweet), attach.
posts = herald_db.get_posts_in_window(conn, since_ts=0, until_ts=int(__import__("time").time() * 1000))
quote_candidate = next(p for p in posts if p["id"] == "2000000000000001")
print(f"  candidate from DB: id={quote_candidate['id']} type={quote_candidate['post_type']}")
print(f"    quoted_tweet stub: {quote_candidate.get('quoted_tweet')}")
assert quote_candidate["quoted_tweet"]["text"] is None, \
    "DB should strip inline text (row-size hygiene)"

herald_context.attach_context_to_candidates([quote_candidate])
qc = quote_candidate.get("_quote_context")
print(f"  attached _quote_context: {qc}")
assert isinstance(qc, list), f"expected list, got {type(qc)}"
assert len(qc) == 3, f"expected 3 chain levels, got {len(qc)}"
expected_texts = [
    "quote-chain depth 1 — points at depth 2",
    "quote-chain depth 2 — points at depth 3",
    "quote-chain depth 3 — chain ends here, no nested quote",
]
for i, item in enumerate(qc):
    level = item["tweet"]
    kind = item["_chain_kind"]
    assert level["id"] == f"100000000000000{i + 1}", f"chain level {i} wrong id"
    assert level["text"] == expected_texts[i], f"chain level {i} wrong text"
    assert kind == "quote", f"chain level {i} should be quote kind"
    print(f"  ✓ chain[{i}] id={level['id']} kind={kind} @{level['author']['username']}")

banner("Test 11: synthesise prompt renders quote-chain depth with ↳ markers")

ctx_posts = herald_synthesise._gather_context_for([quote_candidate])
block, n = herald_synthesise._render_context_block(ctx_posts)
print(f"  rendered ({n} context posts, {len(block)} chars):")
print("  ---")
for line in block.split("\n"):
    print(f"  {line[:140]}")
print("  ---")
# Depth-0 uses •, depth 1+ uses ↳. Count items by the leading "•" or "↳"
# (not "[post:" which appears in the header prose).
assert "•" in block, "top-level context should have bullet marker"
assert "↳" in block, "nested quote levels should have ↳ marker"
lines = block.split("\n")
context_lines = [l for l in lines if l.lstrip().startswith(("•", "↳"))]
print(f"  found {len(context_lines)} context lines")
assert len(context_lines) == 3, f"expected 3 context lines, got {len(context_lines)}"
# Verify the depth markers appear in the right order (depth-0 first, ↳ for rest)
assert context_lines[0].lstrip().startswith("•"), "first context line should use bullet"
assert context_lines[1].lstrip().startswith("↳"), "second context line should use ↳ (depth-1)"
assert context_lines[2].lstrip().startswith("↳"), "third context line should use ↳ (depth-2)"
# Depth-2 should be more indented than depth-1
indent_1 = len(context_lines[1]) - len(context_lines[1].lstrip())
indent_2 = len(context_lines[2]) - len(context_lines[2].lstrip())
assert indent_2 > indent_1, f"depth-2 indent ({indent_2}) should exceed depth-1 ({indent_1})"

banner("Test 12: failed quote fetch is recorded, doesn't abort batch")

# Use the 'broken' fixture: post quotes a tweet whose id the mock doesn't know.
conn.close()
TEST_DB = str(TEST_DIR / f"test-phase12-{int(_t.time())}.db")
os.environ["HERALD_DB_PATH"] = TEST_DB
conn = herald_db.get_connection()
herald_db.init_schema(conn)
conn = herald_db.get_connection()
herald_db.init_schema(conn)

broken_post = {
    "id": "2000000000000099",
    "url": "https://x.com/x/status/2000000000000099",
    "text": "this quote will fail to fetch",
    "author": {"id": "x", "username": "x"},
    "metrics": {"likes": 0, "retweets": 0, "replies": 0, "quote_count": 0},
    "conversation": {"id": "2000000000000099", "parent_id": None, "is_reply": False, "parent_tweet": None},
    # Inline quote has text=None (simulating bird couldn't inline) AND points
    # at an id the mock doesn't recognise — fetch will fail and be recorded.
    "quoted_tweet": {
        # 'FAIL_' prefix triggers a forced not-found in mock_bird, so the
        # fetch raises and is recorded as 'failed'.
        "id": "FAIL_9999999888888",
        "text": None,
        "author": None,
    },
    "media": None, "links": None,
    "created_at": "Thu Jul 02 23:13:00 +0000 2026",
    "lang": "en", "source": "test",
    "post_type": "quote",
}

herald_db.upsert_post(conn, broken_post)
# The inline data has text=None so upsert_post should NOT seed the cache.
unseeded = conn.execute(
    "SELECT 1 FROM quote_context WHERE quoted_tweet_id = 'FAIL_9999999888888'"
).fetchone()
assert unseeded is None, "no inline text → no seed (correct)"
print(f"  ✓ upsert_post did not seed (text=None)")

# Commit before enrich_batch; otherwise the test's open transaction blocks
# enrich_batch's internal connection.
conn.commit()

# enrich_batch should try to fetch, fail, and record 'failed'.
attempted, succeeded, failed, skipped = herald_context.enrich_batch([broken_post])
print(f"  enrich_batch: attempted={attempted} ok={succeeded} fail={failed} skip={skipped}")
# Note: attempted counter only increments on actual fetch attempts. The mock
# bird will exit non-zero for unknown ids → fetch fails → status='failed'.
assert attempted >= 1, f"expected at least 1 fetch attempt, got {attempted}"
failed_row = conn.execute(
    "SELECT fetch_status, fetch_error FROM quote_context WHERE quoted_tweet_id = 'FAIL_9999999888888'"
).fetchone()
assert failed_row is not None
assert failed_row["fetch_status"] == "failed"
print(f"  ✓ failed fetch recorded: status={failed_row['fetch_status']} err={failed_row['fetch_error'][:80]}")

# Attach should not produce _quote_context for this candidate (no successful row).
posts = herald_db.get_posts_in_window(conn, since_ts=0, until_ts=int(__import__("time").time() * 1000))
broken_cand = next(p for p in posts if p["id"] == "2000000000000099")
herald_context.attach_context_to_candidates([broken_cand])
assert "_quote_context" not in broken_cand, \
    "candidate with failed quote fetch should have no _quote_context"
print(f"  ✓ attach correctly skipped candidate with failed fetch")

# ---------- Reply-parent traversal tests (Phase 3.5.1: James's pattern) ----------

banner("Test 13: enrich_batch unravels quote-of-reply-of-quote-of-... chain")

# Re-init test DB so this section is self-contained.
conn.close()
TEST_DB = str(TEST_DIR / f"test-phase13-{int(_t.time())}.db")
os.environ["HERALD_DB_PATH"] = TEST_DB
importlib.reload(herald_db)
herald_db = importlib.reload(herald_db)
conn = herald_db.get_connection()
herald_db.init_schema(conn)

# Construct the top tweet: quote-tweets B (the first level).
# Bird's inline quotedTweet includes B's text + author + reply metadata (so the
# inline seed for B has conversation.parent_id set).
top_tweet = {
    "id": "2000000000000001",
    "url": "https://x.com/author_a/status/2000000000000001",
    "text": "Big if true",
    "author": {"id": "2010", "username": "author_a"},
    "metrics": {"likes": 0, "retweets": 0, "replies": 0, "quote_count": 0},
    "conversation": {"id": "2000000000000001", "parent_id": None, "is_reply": False, "parent_tweet": None},
    "quoted_tweet": {
        "id": "2000000000000002",
        "url": "https://x.com/reply_author_b/status/2000000000000002",
        "text": "REPLY-1: @author_c one of the most bizarre experiences...",
        "author": {"username": "reply_author_b", "name": "Reply B"},
        "created_at": "Thu Jul 02 23:10:00 +0000 2026",
        "metrics": {"likes": 0, "retweets": 0, "replies": 0, "quote_count": 0},
        # No nested quotedTweet (bird's --quote-depth 1 doesn't expose it).
        # BUT the inline seed includes conversation metadata for B.
        "conversation": {
            "id": "2000000000000003",
            "parent_id": "2000000000000003",
            "is_reply": True,
            "parent_tweet": None,
        },
    },
    "media": None, "links": None,
    "created_at": "Thu Jul 02 23:11:00 +0000 2026",
    "lang": "en", "source": "test",
    "post_type": "quote",
}

# Upsert (seeds inline quote with reply metadata).
herald_db.upsert_post(conn, top_tweet)
conn.commit()

# enrich_batch should:
#   - find B in cache (inline seed), skip fetch
#   - chase B's reply parent → fetch C (via reply chain)
#   - chase C's quotedTweet → fetch D (via quote chain)
#   - chase D's reply parent → fetch E (via reply chain)
#   - chase E's quotedTweet → fetch F (via quote chain)
#   - chase F's quotedTweet → fetch G (via quote chain, terminal)
attempted, succeeded, failed, skipped = herald_context.enrich_batch([top_tweet])
print(f"  enrich_batch: attempted={attempted} ok={succeeded} fail={failed} skip={skipped}")
# Expected: 5 fetches (C, D, E, F, G — B was inline-seeded).
assert attempted == 5, f"expected 5 fetches for full chain, got {attempted}"
assert succeeded == 5, "all 5 should succeed against mock"

# Verify all levels are cached with correct provenance.
expected_chain = [
    ("2000000000000002", "inline-from-bird",     "B"),
    ("2000000000000003", "fetched-via-reply-chain", "C"),
    ("2000000000000004", "fetched-via-quote-chain", "D"),
    ("2000000000000005", "fetched-via-reply-chain", "E"),
    ("2000000000000006", "fetched-via-quote-chain", "F"),
    ("2000000000000007", "fetched-via-quote-chain", "G"),
]
for qid, src, label in expected_chain:
    row = conn.execute(
        "SELECT fetch_status, fetch_error FROM quote_context WHERE quoted_tweet_id = ?",
        (qid,),
    ).fetchone()
    assert row is not None, f"{label} ({qid}) missing from cache"
    assert row["fetch_status"] == "success"
    assert row["fetch_error"] == src, \
        f"{label} provenance: expected '{src}', got '{row['fetch_error']}'"
    print(f"  ✓ {label} ({qid}): {src}")

conn.commit()

banner("Test 14: attach emits interleaved quote/reply chain with kinds")

posts = herald_db.get_posts_in_window(conn, since_ts=0, until_ts=int(_t.time() * 1000))
top_cand = next(p for p in posts if p["id"] == "2000000000000001")
herald_context.attach_context_to_candidates([top_cand])
qc = top_cand.get("_quote_context")
assert qc is not None, "expected _quote_context attached"
print(f"  chain length: {len(qc)}")
for i, item in enumerate(qc):
    tw = item["tweet"]
    kind = item["_chain_kind"]
    author = (tw.get("author") or {}).get("username", "?")
    print(f"    [{i}] {tw['id']} kind={kind} @{author}")

# Expected order (DFS through quote + reply dims, starting with B):
#   [0] B kind=quote  (depth-1 quote target of top)
#   [1] C kind=reply  (B's reply parent)
#   [2] D kind=quote  (C's quotedTweet)
#   [3] E kind=reply  (D's reply parent)
#   [4] F kind=quote  (E's quotedTweet)
#   [5] G kind=quote  (F's quotedTweet, the source)
expected = [
    ("2000000000000002", "quote"),
    ("2000000000000003", "reply"),
    ("2000000000000004", "quote"),
    ("2000000000000005", "reply"),
    ("2000000000000006", "quote"),
    ("2000000000000007", "quote"),
]
assert len(qc) == len(expected), f"chain len mismatch: got {len(qc)}, want {len(expected)}"
for i, (qid, kind) in enumerate(expected):
    assert qc[i]["tweet"]["id"] == qid, f"chain[{i}] id mismatch"
    assert qc[i]["_chain_kind"] == kind, f"chain[{i}] kind mismatch: expected {kind}, got {qc[i]['_chain_kind']}"
print(f"  ✓ chain order + kinds match expected pattern (the full unravel)")

banner("Test 15: rendering shows ⤴ for reply edges and ↳ for quote depth")

ctx_posts = herald_synthesise._gather_context_for([top_cand])
block, n = herald_synthesise._render_context_block(ctx_posts)
print(f"  rendered ({n} items, {len(block)} chars):")
print("  ---")
for line in block.split("\n"):
    print(f"  {line[:140]}")
print("  ---")
assert "•" in block, "top-level context should have bullet marker"
assert "↳" in block, "nested quote levels should have ↳ marker"
assert "⤴" in block, "reply parents should have ⤴ marker"
lines = block.split("\n")
context_lines = [l for l in lines if l.lstrip().startswith(("•", "↳", "⤴"))]
assert len(context_lines) == 6, f"expected 6 context lines, got {len(context_lines)}"
# Reply lines use ⤴, quote lines use • or ↳
reply_lines = [l for l in context_lines if l.lstrip().startswith("⤴")]
quote_lines = [l for l in context_lines if l.lstrip().startswith(("•", "↳"))]
assert len(reply_lines) == 2, f"expected 2 reply edges, got {len(reply_lines)}"
assert len(quote_lines) == 4, f"expected 4 quote levels, got {len(quote_lines)}"
print(f"  ✓ {len(quote_lines)} quote levels + {len(reply_lines)} reply edges rendered")

banner("Test 16: HERALD_FOLLOW_REPLY_PARENTS=0 collapses reply edges")

conn.close()
TEST_DB = str(TEST_DIR / f"test-phase16-{int(_t.time())}.db")
os.environ["HERALD_DB_PATH"] = TEST_DB
os.environ["HERALD_FOLLOW_REPLY_PARENTS"] = "0"
# Force module reload so the new env var takes effect.
importlib.reload(herald_db)
herald_db = importlib.reload(herald_db)
importlib.reload(herald_context)
herald_context = importlib.reload(herald_context)
importlib.reload(herald_synthesise)
herald_synthesise = importlib.reload(herald_synthesise)

assert herald_context.HERALD_FOLLOW_REPLY_PARENTS is False, "toggle should be off"
conn = herald_db.get_connection()
herald_db.init_schema(conn)
herald_db.upsert_post(conn, top_tweet)
conn.commit()
attempted, succeeded, failed, skipped = herald_context.enrich_batch([top_tweet])
print(f"  enrich_batch (reply-parents disabled): attempted={attempted} ok={succeeded} fail={failed} skip={skipped}")
# Without reply chase, walker from B sees no quotedTweet → stops. Only B is in chain.
assert attempted == 0, f"expected 0 fetches with reply-parents disabled, got {attempted}"
posts = herald_db.get_posts_in_window(conn, since_ts=0, until_ts=int(_t.time() * 1000))
top_cand = next(p for p in posts if p["id"] == "2000000000000001")
herald_context.attach_context_to_candidates([top_cand])
qc = top_cand.get("_quote_context", [])
assert len(qc) == 1, f"with reply-parents off, chain should be just B, got {len(qc)} items"
assert qc[0]["_chain_kind"] == "quote"
assert qc[0]["tweet"]["id"] == "2000000000000002"
print(f"  ✓ toggle off → reply chain collapses, only inline B in context")
os.environ["HERALD_FOLLOW_REPLY_PARENTS"] = "1"  # restore for any later tests

banner("Test 17: estimate_context_cost returns sensible numbers")

conn.close()
TEST_DB = str(TEST_DIR / f"test-phase17-{int(_t.time())}.db")
os.environ["HERALD_DB_PATH"] = TEST_DB
importlib.reload(herald_context)
herald_context = importlib.reload(herald_context)
importlib.reload(herald_db)
herald_db = importlib.reload(herald_db)
conn = herald_db.get_connection()
herald_db.init_schema(conn)
herald_db.upsert_post(conn, top_tweet)
conn.commit()
# Pre-populate cache via enrich_batch so the estimator can see the full
# chain structure (this is the realistic admin-panel use case: compare
# settings against already-collected data).
attempted, succeeded, failed, skipped = herald_context.enrich_batch([top_tweet])
conn.commit()
print(f"  enrich_batch result: attempted={attempted} succeeded={succeeded} HERALD_FOLLOW_REPLY_PARENTS={herald_context.HERALD_FOLLOW_REPLY_PARENTS} BIRD_BIN={herald_context.BIRD_BIN}")
print(f"  HERALD_BIRD_BIN env: {os.environ.get('HERALD_BIRD_BIN')}")
assert succeeded == 5, "expected 5 fetches to populate cache"

est = herald_context.estimate_context_cost([top_tweet])
print(f"  estimate: {est}")
assert est["quote_posts"] == 1
# All 5 items are cached, so estimator should see ZERO additional fetches.
assert est["fetch_attempts"] == 0, \
    f"with cache populated, expected 0 additional fetches, got {est['fetch_attempts']}"
# But it should still see all 6 items in the projected context.
assert est["context_tweets_attached"] == 6, \
    f"expected 6 context items, got {est['context_tweets_attached']}"
assert est["approx_input_tokens"] == 6 * 120
print(f"  ✓ cost estimator matches actual behaviour with cache populated")

# Now test the OTHER mode: estimator on a fresh batch with empty cache.
# Run on a SECOND copy of the tweet (different id) so the cache is fresh.
conn.close()
TEST_DB = str(TEST_DIR / f"test-phase17b-{int(_t.time())}.db")
os.environ["HERALD_DB_PATH"] = TEST_DB
importlib.reload(herald_context)
herald_context = importlib.reload(herald_context)
importlib.reload(herald_db)
herald_db = importlib.reload(herald_db)
conn = herald_db.get_connection()
herald_db.init_schema(conn)
fresh_top = dict(top_tweet)
fresh_top["id"] = "2999999999999999"
herald_db.upsert_post(conn, fresh_top)
conn.commit()
est_fresh = herald_context.estimate_context_cost([fresh_top])
print(f"  fresh-cache estimate: {est_fresh}")
# Only the inline seed is in cache; the walker sees B (cached) but doesn't
# know about C/D/E/F/G structure without seeing C's data. Conservative
# estimate: 1 fetch (just for C, the immediate reply parent).
assert est_fresh["fetch_attempts"] >= 1, \
    f"fresh-cache estimate should predict >=1 fetch, got {est_fresh['fetch_attempts']}"
print(f"  ✓ fresh-cache estimate is conservative (>=1 fetch)")

print("\nAll tests pass. ✓")
conn.close()