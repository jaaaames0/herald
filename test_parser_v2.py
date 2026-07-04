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

Run from the herald dir:
    HERALD_DB_PATH=/tmp/herald-test/test.db python3 test_parser_v2.py

Requires the companion test_mock_bird executable in the same directory.
The /tmp location keeps the test away from /var/lib/herald/ on chicken
and from optiplex's live DB.
"""
import json
import os
import re
import sys
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
        if isinstance(qc, list):
            qc_info = f"chain len={len(qc)} ids={[t['id'] for t in qc]}"
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
import time as _t
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
for i, level in enumerate(qc):
    assert level["id"] == f"100000000000000{i + 1}", f"chain level {i} wrong id"
    assert level["text"] == expected_texts[i], f"chain level {i} wrong text"
    print(f"  ✓ chain[{i}] id={level['id']} @{level['author']['username']}")

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

print("\nAll tests pass. ✓")
conn.close()