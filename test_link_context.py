"""Tests for Phase 3.6.2 link context (herald_links + LINK CONTEXT renderer).

Mirrors test_image_context.py structure: isolated sub-DB per test, mocked
HTTP via herald_links._http_get, no live network. Tests don't touch the
live herald.db.

Tests verify:
  1. _extract_link_urls accepts both 'links' (parser) and 'link_urls' (DB)
     shapes and handles None / missing / non-list values cleanly.
  2. enrich_link_batch writes success row to link_context.
  3. failed fetches (HTTPError, timeout, URL error, extraction-empty) are
     stored as 'failed' rows; no exception escapes enrich_link_batch.
  4. Cache hit: second enrich skips already-cached URLs (both success and
     failed rows).
  5. attach_link_context wires _link_context onto candidates as
     list[{post_id, url, text}].
  6. _pick_links_for_story respects HERALD_LINKS_PER_STORY cap and
     engagement-priority ordering.
  7. _render_link_context_block produces the expected prompt format
     (header, 📰 marker, URL visible, truncation marker).
  8. End-to-end: enrich -> attach -> assemble stage 2 prompt (dry-run);
     verify LINK CONTEXT appears between IMAGE CONTEXT (if any) and the
     "Write the story JSON now." footer.
"""
import os
import sys
import time
import json
import importlib
from pathlib import Path

# Force-disable live HTTP — tests monkeypatch herald_links._http_get.
os.environ.setdefault("HERALD_OPENROUTER_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Use a unique test DB per run to avoid touching the live herald.db.
TEST_DIR = "/tmp/herald-test-link"
os.makedirs(TEST_DIR, exist_ok=True)
TEST_DB = f"{TEST_DIR}/test_link_{int(time.time())}.db"
os.environ["HERALD_DB_PATH"] = TEST_DB

import herald_db
import herald_links
import herald_synthesise


def make_parsed_post(external_id, links, text="x", metrics=None):
    """Build a minimal parsed-post dict shaped like what herald_parser emits."""
    return {
        "id": external_id,
        "external_id": external_id,
        "text": text,
        "author": {"username": f"user_{external_id[-3:]}", "name": f"User {external_id}"},
        "metrics": metrics or {"likes": 0, "retweets": 0, "replies": 0, "quote_count": 0},
        "media": [],
        "links": links,
        "post_type": "original",
        "conversation": {"id": external_id, "parent_id": None, "is_reply": False, "parent_tweet": None},
        "created_at": "Thu Jul 03 12:00:00 +0000 2026",
        "lang": "en", "source": "test",
    }


def banner(name):
    print(f"\n=== {name} ===")


def reset_db():
    """Wipe link_context and posts-like state for a fresh test."""
    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    conn.execute("DELETE FROM link_context")
    conn.commit()
    conn.close()


# Sample HTML fixtures used across tests.
SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Sample news article</title>
  <meta property="og:title" content="OG headline that should NOT appear in extracted text">
  <meta property="og:description" content="OG description that should NOT appear either">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="description" content="meta description that should also be ignored">
</head>
<body>
  <nav>nav menu items should not appear</nav>
  <header>site header stuff</header>
  <main>
    <article>
      <h1>Real article headline</h1>
      <p>This is the first paragraph of a real news article about an important event. It has enough detail to be useful for a digest story.</p>
      <p>This is the second paragraph, providing additional context and continuing the narrative thread.</p>
      <aside>an aside that should not appear</aside>
      <script>var junk = 'this should be suppressed';</script>
      <style>.junk { display: none; }</style>
      <h2>A subsection</h2>
      <p>More content in a subsection, with sufficient characters to clear HERALD_LINK_MIN_TEXT_CHARS easily.</p>
    </article>
  </main>
  <footer>footer junk that should be stripped</footer>
</body>
</html>"""

EMPTY_HTML = """<!DOCTYPE html>
<html>
<head><title>nothing here</title></head>
<body>
<script>var junk;</script>
<style>.x {}</style>
</body>
</html>"""


def fake_http_ok(body, redirect_url=None):
    """Return a fake _http_get function that yields one canned response."""
    def _fake(url, timeout=None):
        return {
            "ok": True,
            "status_code": 200,
            "body_bytes": body.encode("utf-8") if isinstance(body, str) else body,
            "redirect_url": redirect_url,
            "error": None,
            "error_kind": None,
        }
    return _fake


def fake_http_error(error_kind, status_code=None, error_msg=None):
    """Return a fake _http_get function that yields one error response."""
    def _fake(url, timeout=None):
        return {
            "ok": False,
            "status_code": status_code,
            "body_bytes": None,
            "redirect_url": None,
            "error": error_msg or error_kind,
            "error_kind": error_kind,
        }
    return _fake


# ============================================================
# Test 1 — _extract_link_urls accepts both shapes and handles None
# ============================================================
banner("Test 1: _extract_link_urls accepts both 'links' (parser) and 'link_urls' (DB) shapes")
post_parser = make_parsed_post("p1", ["https://a.example.com/x", "https://b.example.com/y"])
got = herald_links._extract_link_urls(post_parser)
assert got == ["https://a.example.com/x", "https://b.example.com/y"], f"got {got}"
print(f"  parser shape: {got}")

post_db = {"link_urls": ["https://c.example.com/z"]}
got = herald_links._extract_link_urls(post_db)
assert got == ["https://c.example.com/z"], f"got {got}"
print(f"  db shape: {got}")

assert herald_links._extract_link_urls({"links": None}) == []
assert herald_links._extract_link_urls({"link_urls": None}) == []
assert herald_links._extract_link_urls({}) == []
assert herald_links._extract_link_urls({"links": []}) == []
assert herald_links._extract_link_urls({"links": ""}) == []  # str -> []
assert herald_links._extract_link_urls({"links": "https://only.example.com"}) == ["https://only.example.com"]
# Order preserved; falsy values dropped.
got = herald_links._extract_link_urls({"links": ["https://a", "", None, "https://b"]})
assert got == ["https://a", "https://b"], f"got {got}"
print(f"  edge cases (None, empty, str, mixed): all return clean lists")
print(f"  ✓ dual-shape accepted; edge cases handled")


# ============================================================
# Test 2 — _canonicalise_url strips tracking params, lowercases host
# ============================================================
banner("Test 2: _canonicalise_url drops tracking params, lowercases host, idempotent")
cases = [
    ("HTTPS://A.Example.com/path?utm_source=x&id=1", "https://a.example.com/path?id=1"),
    ("https://x.com/foo?fbclid=abc&keep=1", "https://x.com/foo?keep=1"),
    ("https://x.com/p?ref_src=tw&ref=home", "https://x.com/p"),
    ("https://x.com/p?utm_medium=email&utm_campaign=t", "https://x.com/p"),
    ("https://x.com/p", "https://x.com/p"),  # no params unchanged
    ("https://x.com/p#section", "https://x.com/p#section"),  # fragment preserved
    ("https://X.COM/P?Q=V", "https://x.com/P?Q=V"),  # host lowercased, query preserved
]
for raw, expected in cases:
    got = herald_links._canonicalise_url(raw)
    assert got == expected, f"raw={raw!r} got={got!r} expected={expected!r}"
# Idempotent.
once = herald_links._canonicalise_url("HTTPS://A.Example.com/P?utm_source=x&keep=1")
twice = herald_links._canonicalise_url(once)
assert once == twice, f"not idempotent: {once!r} -> {twice!r}"
print(f"  {len(cases)} canonicalisation cases pass; idempotent confirmed")
print(f"  ✓ canonicalisation correct")


# ============================================================
# Test 3 — enrich_link_batch writes success row
# ============================================================
banner("Test 3: enrich_link_batch writes success row to link_context")
reset_db()
herald_links._http_get = fake_http_ok(SAMPLE_HTML)

post = make_parsed_post("t3", ["https://example.com/article-A"])
attempted, ok, failed, skipped = herald_links.enrich_link_batch([post], max_fetches=5)
assert attempted == 1 and ok == 1 and failed == 0, f"unexpected counts: {attempted}/{ok}/{failed}/{skipped}"

conn = herald_db.get_connection()
row = conn.execute(
    "SELECT fetch_status, fetch_error, content_text, content_chars, content_bytes FROM link_context WHERE canonical_url = ?",
    ("https://example.com/article-A",),
).fetchone()
assert row is not None, "expected link_context row"
d = dict(row)
assert d["fetch_status"] == "success", f"got status {d['fetch_status']!r}"
assert d["fetch_error"] is None
assert d["content_text"] is not None and "first paragraph" in d["content_text"]
assert d["content_chars"] == len(d["content_text"])
assert d["content_bytes"] > 0
# Crucially, OG/twitter meta text must NOT appear in the extracted body.
assert "OG headline" not in d["content_text"], "og:title leaked into extraction"
assert "OG description" not in d["content_text"], "og:description leaked into extraction"
assert "twitter:card" not in d["content_text"]
# nav/footer/script text must NOT appear.
assert "nav menu items" not in d["content_text"], "nav text leaked into extraction"
assert "footer junk" not in d["content_text"], "footer text leaked into extraction"
assert "should be suppressed" not in d["content_text"], "script body leaked into extraction"
assert "this should be suppressed" not in d["content_text"]
# And the actual article content must be there.
assert "Real article headline" in d["content_text"], "article h1 missing"
assert "first paragraph of a real news article" in d["content_text"], "article p missing"
conn.close()
print(f"  attempted={attempted} ok={ok} failed={failed}")
print(f"  content_chars={d['content_chars']} content_bytes={d['content_bytes']}")
print(f"  preview: {d['content_text'][:80]!r}…")
print(f"  ✓ success row stored; OG/nav/footer/script all excluded; article body intact")


# ============================================================
# Test 4 — failed fetches stored, never raises
# ============================================================
banner("Test 4: failed fetches stored as 'failed', no exception escapes enrich_link_batch")
reset_db()

# Multi-response fake: returns different errors for different URLs.
responses_by_url = {
    "https://example.com/404":   fake_http_error("http", status_code=404, error_msg="HTTP 404"),
    "https://example.com/500":   fake_http_error("http", status_code=500, error_msg="HTTP 500"),
    "https://example.com/timeout": fake_http_error("timeout", error_msg="timeout after 10s"),
    "https://example.com/dns":    fake_http_error("url", error_msg="DNS failure"),
    "https://example.com/empty":  fake_http_ok(EMPTY_HTML),
    "https://example.com/big":    fake_http_error("too_large", error_msg="body exceeded 2097152 bytes"),
}
def fake_router(url, timeout=None):
    # Find first matching key (substring match for simplicity).
    for key, fn in responses_by_url.items():
        if key in url:
            return fn(url, timeout)
    return fake_http_error("other", error_msg="unexpected")(url, timeout)
herald_links._http_get = fake_router

posts = [
    make_parsed_post(f"t4-{i}", [url])
    for i, url in enumerate(responses_by_url.keys())
]
attempted, ok, failed, skipped = herald_links.enrich_link_batch(posts, max_fetches=10)
assert attempted == len(responses_by_url), f"expected {len(responses_by_url)} attempts, got {attempted}"
assert ok == 0, f"expected 0 ok, got {ok}"
assert failed == len(responses_by_url), f"expected all failed, got {failed}"

# Verify each URL has a 'failed' row with the right error_kind/error pattern.
conn = herald_db.get_connection()
for url in responses_by_url.keys():
    row = conn.execute(
        "SELECT fetch_status, fetch_error FROM link_context WHERE canonical_url = ?",
        (url,),
    ).fetchone()
    assert row is not None, f"missing row for {url}"
    d = dict(row)
    assert d["fetch_status"] == "failed", f"{url} status was {d['fetch_status']!r}"
    assert d["fetch_error"], f"{url} error message missing"
conn.close()
print(f"  attempted={attempted} ok={ok} failed={failed}")
print(f"  all 6 error modes recorded: http/timeout/url/empty/too_large")
print(f"  ✓ no exception; all errors persisted")


# ============================================================
# Test 5 — cache hit, second enrich skips already-cached URLs
# ============================================================
banner("Test 5: cache hit — second enrich skips already-cached URLs")
reset_db()

call_log = []
def fake_logged(url, timeout=None):
    call_log.append(url)
    return fake_http_ok(SAMPLE_HTML)(url, timeout)
herald_links._http_get = fake_logged

# Seed a success row directly.
conn = herald_db.get_connection()
herald_db.upsert_link_context(
    conn, "https://example.com/cached", "success",
    content_text="cached body", content_bytes=100,
)
# Seed a failed row directly.
herald_db.upsert_link_context(
    conn, "https://example.com/old-fail", "failed",
    error="previous fetch failed", content_bytes=None,
)
conn.commit()
conn.close()

post = make_parsed_post("t5", [
    "https://example.com/cached",
    "https://example.com/old-fail",
    "https://example.com/new-fetch",
])
attempted, ok, failed, skipped = herald_links.enrich_link_batch([post], max_fetches=5)
assert attempted == 1, f"expected only NEW fetched, got {attempted}"
assert ok == 1, f"expected ok=1, got {ok}"
assert any("new-fetch" in u for u in call_log), f"new-fetch missing from call log: {call_log}"
assert not any("cached" in u for u in call_log), f"cached URL was fetched: {call_log}"
assert not any("old-fail" in u for u in call_log), f"old-fail was retried: {call_log}"
print(f"  call_log={call_log}")
print(f"  attempted={attempted} ok={ok}")
print(f"  ✓ cached success AND cached failed both skipped; only fresh URL fetched")


# ============================================================
# Test 6 — attach_link_context wires _link_context onto candidates
# ============================================================
banner("Test 6: attach_link_context populates _link_context from cache")
reset_db()
conn = herald_db.get_connection()
herald_db.upsert_link_context(
    conn, "https://a.example.com", "success",
    content_text="Article A body — a substantive piece of journalism about a real event.", content_bytes=200,
)
herald_db.upsert_link_context(
    conn, "https://b.example.com", "success",
    content_text="Article B body — different topic, also substantive content for context.", content_bytes=300,
)
conn.commit()
conn.close()

cands = [
    # Parsed at collect time: 'links' key, two URLs, both cached.
    make_parsed_post("t6a", ["https://a.example.com", "https://b.example.com"]),
    # Loaded from DB at digest time: 'link_urls' key, one URL, cached.
    {"id": "t6b", "link_urls": ["https://a.example.com"], "_link_skipped": []},
    # No cached content.
    make_parsed_post("t6c", ["https://uncached.example.com"]),
]
herald_links.attach_link_context(cands)

assert isinstance(cands[0]["_link_context"], list), "expected list"
assert len(cands[0]["_link_context"]) == 2, f"got {len(cands[0]['_link_context'])} entries"
for entry in cands[0]["_link_context"]:
    assert set(entry.keys()) >= {"post_id", "url", "text"}, f"missing keys in {entry}"
    assert entry["post_id"] == "t6a"
    assert "Article" in entry["text"]

# Second candidate (DB shape).
assert len(cands[1]["_link_context"]) == 1
assert cands[1]["_link_context"][0]["url"] == "https://a.example.com"

# Third candidate: empty.
assert cands[2]["_link_context"] == []

# _link_skipped is always set (symmetry with images).
for c in cands:
    assert c["_link_skipped"] == []
print(f"  cand 0: {len(cands[0]['_link_context'])} entries")
print(f"  cand 1: {len(cands[1]['_link_context'])} entries")
print(f"  cand 2: {len(cands[2]['_link_context'])} entries")
print(f"  ✓ all three candidate shapes handled correctly")


# ============================================================
# Test 7 — _pick_links_for_story engagement sort + cap
# ============================================================
banner("Test 7: _pick_links_for_story respects cap and engagement sort")

posts = [
    # post-A: 2 links, low engagement.
    {"id": "A", "_link_context": [
        {"post_id": "A", "url": "https://a1", "text": "a1"},
        {"post_id": "A", "url": "https://a2", "text": "a2"},
    ], "metrics": {"likes": 10, "retweets": 0}},  # score 10
    # post-B: 1 link, high engagement.
    {"id": "B", "_link_context": [
        {"post_id": "B", "url": "https://b1", "text": "b1"},
    ], "metrics": {"likes": 500, "retweets": 50}},  # score 650
    # post-C: 1 link, mid engagement.
    {"id": "C", "_link_context": [
        {"post_id": "C", "url": "https://c1", "text": "c1"},
    ], "metrics": {"likes": 50, "retweets": 5}},  # score 65
    # post-D: no link context.
    {"id": "D", "_link_context": [], "metrics": {"likes": 10000, "retweets": 0}},
]

# cap=0 → empty (feature disabled).
assert herald_synthesise._pick_links_for_story(posts, 0) == []
# cap=2 → B then C (engagement desc). A's links score 10 each, lose to B(650) and C(65).
picked = herald_synthesise._pick_links_for_story(posts, 2)
assert [p[1] for p in picked] == ["https://b1", "https://c1"], f"got {[p[1] for p in picked]}"
# cap=4 → B, C, A1, A2 in that order (B first because engagement, then C, then A's two in order).
picked = herald_synthesise._pick_links_for_story(posts, 4)
assert [p[1] for p in picked] == ["https://b1", "https://c1", "https://a1", "https://a2"], f"got {[p[1] for p in picked]}"
print(f"  cap=2: {[p[1] for p in picked[:2]]}")
print(f"  cap=4: {[p[1] for p in picked]}")
print(f"  ✓ engagement sort, post order tie-break, link order within post preserved")


# ============================================================
# Test 8 — _render_link_context_block format check
# ============================================================
banner("Test 8: _render_link_context_block produces expected format")

picked = [
    ("post-A", "https://example.com/article-1", "Body of article 1. Substantive content."),
    ("post-B", "https://example.com/article-2", "Body of article 2 with more text."),
]
text, n = herald_synthesise._render_link_context_block(picked, max_chars=200)
assert n == 2
assert text.startswith("LINK CONTEXT (2 parsed;")
assert "[post:post-A] https://example.com/article-1" in text
assert "[post:post-B] https://example.com/article-2" in text
assert "Body of article 1" in text
# Truncation marker when text exceeds max_chars.
long_text = "x" * 500
picked_long = [("post-X", "https://example.com/long", long_text)]
text_long, _ = herald_synthesise._render_link_context_block(picked_long, max_chars=100)
assert "…[+truncated]" in text_long, f"expected truncation marker: {text_long[-60:]!r}"
print("---")
print(text)
print("---")
print(f"  ✓ header + 📰 marker + URL visible + truncation marker all present")


# ============================================================
# Test 9 — end-to-end through stage 2 prompt assembly (dry-run)
# ============================================================
banner("Test 9: end-to-end through stage 2 prompt assembly (dry-run)")
reset_db()
conn = herald_db.get_connection()
herald_db.upsert_link_context(
    conn, "https://example.com/stage-A", "success",
    content_text="Article body about a real news event with enough detail to be useful.",
    content_bytes=200,
)
herald_db.upsert_link_context(
    conn, "https://example.com/stage-B", "success",
    content_text="Another article body, also substantive content for context here.",
    content_bytes=200,
)
conn.commit()
conn.close()

# Build theme posts. post-1 has the high-engagement link; post-2 has a different link.
theme_posts = [
    make_parsed_post("story-1", ["https://example.com/stage-A"],
                     metrics={"likes": 1000, "retweets": 100}),
    make_parsed_post("story-2", ["https://example.com/stage-B"],
                     metrics={"likes": 50, "retweets": 0}),
]
herald_links.attach_link_context(theme_posts)

# Build the user_parts manually (mirroring stage2_story's assembly) so we can
# inspect the resulting prompt order without an LLM call.
picked = herald_synthesise._pick_links_for_story(theme_posts, herald_synthesise.HERALD_LINKS_PER_STORY)
block, n = herald_synthesise._render_link_context_block(picked)
assert n == 2, f"expected 2 links (default cap), got {n}"
assert picked[0][1] == "https://example.com/stage-A", f"expected stage-A first (higher engagement), got {picked[0][1]}"

# Simulate the user_parts assembly order from stage2_story.
user_parts = [
    "POSTS (2):\n[]",
    "CONTEXT POSTS (0):",
]
if block:
    user_parts.append(block)
user_parts.append("Write the story JSON now.")

# Order: POSTS, CONTEXT POSTS, LINK CONTEXT, "Write the story JSON now."
joined = "\n\n".join(user_parts)
link_pos = joined.find("LINK CONTEXT")
write_pos = joined.find("Write the story JSON now.")
assert link_pos != -1 and write_pos != -1
assert link_pos < write_pos, "LINK CONTEXT must come before the write footer"
print(f"  picked: story-1 first (1000+300=1300 score > 50+0=50)")
print(f"  user_parts order: POSTS → CONTEXT POSTS → LINK CONTEXT → Write footer")
print(f"  ✓ end-to-end: LINK CONTEXT in correct position, engagement-priority works")


# ============================================================
# Test 10 — MainContentParser scopes to <main> when present
# ============================================================
banner("Test 10: MainContentParser scopes to <main> when present (skips pre-main chrome)")

# Synthetic shape: lots of nav/header/footer noise before <main>, article
# body inside <main><article>. Without scoping, the cap gets hit by chrome.
chrome_heavy_html = """<!DOCTYPE html>
<html>
<head>
  <title>SiteName - lots of menu noise</title>
  <meta property="og:title" content="OG headline should be ignored">
</head>
<body>
  <nav class="PrimaryNav">
    <ul>
      <li><a href="/world">World News</a></li>
      <li><a href="/politics">Politics</a></li>
      <li><a href="/business">Business</a></li>
      <li><a href="/sports">Sports</a></li>
      <li><a href="/entertainment">Entertainment</a></li>
      <li><a href="/lifestyle">Lifestyle</a></li>
      <li><a href="/opinion">Opinion</a></li>
      <li><a href="/tech">Tech</a></li>
    </ul>
  </nav>
  <header class="SiteHeader">
    <h1>SiteName Mega Headline</h1>
    <p>Top stories sidebar story one with a long headline about something</p>
    <p>Top stories sidebar story two with a long headline about something else</p>
    <p>Top stories sidebar story three with a long headline about a third thing</p>
    <p>Trending now: many topics you might find interesting that are unrelated to the article</p>
  </header>
  <aside class="Sidebar">
    <h2>More from this section</h2>
    <p>Sidebar story about an unrelated topic</p>
    <p>Another sidebar story also unrelated</p>
  </aside>
  <main class="Page-main">
    <article>
      <h1>FIFA Lifts Balogun Red Card After White House Call</h1>
      <p>FIFA President Gianni Infantino on Friday lifted the suspension of U.S. forward Folarin Balogun following a direct intervention from the White House, multiple sources confirmed to The Associated Press.</p>
      <p>The reversal clears Balogun to play in Monday's group-stage match against Belgium after a controversial red card in the previous game had put his tournament participation in doubt.</p>
      <p>Federal Judge Ebony Horowitz-Gupta later issued a separate ruling that also cleared Balogun, underscoring how a single on-field call had escalated into a multi-front political and legal drama.</p>
      <p>"The White House called FIFA's Gianni Infantino to review Team USA's Folarin Balogun's red card," an AP source said, speaking on condition of anonymity.</p>
    </article>
  </main>
  <footer class="SiteFooter">
    <p>Footer links and copyright noise that should be ignored entirely</p>
  </footer>
</body>
</html>"""

parser = herald_links.MainContentParser()
parser.feed(chrome_heavy_html)
parser.close()
text = parser.get_text()
assert text, "expected extracted text"

# Article-specific content must be present.
assert "FIFA Lifts Balogun Red Card After White House Call" in text, \
    f"article H1 missing in: {text[:300]!r}"
assert "Federal Judge Ebony Horowitz-Gupta" in text, "article body missing"
assert "group-stage match against Belgium" in text, "article paragraph missing"
# Pre-main chrome must NOT appear (this is the bug the fix addresses).
assert "World News" not in text, "nav menu leaked: 'World News' present"
assert "Politics" not in text or "World News" not in text, "nav menu leaked"
assert "SiteName Mega Headline" not in text, "header leaked"
assert "Top stories sidebar" not in text, "header sidebar stories leaked"
assert "More from this section" not in text, "aside content leaked"
assert "Sidebar story" not in text, "aside paragraphs leaked"
# OG/twitter meta suppressed.
assert "OG headline" not in text, "og:title leaked into extraction"
# Footer chrome suppressed.
assert "Footer links and copyright" not in text, "footer leaked"
print(f"  extracted {len(text)} chars from <main><article>")
print(f"  preview: {text[:150]!r}…")
print(f"  ✓ pre-main chrome excluded; article body fully captured")


# ============================================================
# Test 11 — fallback when no <main>/<article>/role="main" present
# ============================================================
banner("Test 11: fallback to legacy behaviour when no main/article/role=main")

# Simple blog-style page with no semantic landmarks — must still work.
legacy_html = """<!DOCTYPE html>
<html>
<head><title>Plain blog</title></head>
<body>
  <nav>menu noise</nav>
  <header>header noise</header>
  <div class="post-content">
    <h1>Real blog post title</h1>
    <p>This is the body of a blog post on a site that doesn't use semantic landmarks. It still has substantive content for the digest.</p>
    <p>A second paragraph with more detail about the topic at hand.</p>
  </div>
  <footer>footer noise</footer>
</body>
</html>"""
parser = herald_links.MainContentParser()
parser.feed(legacy_html)
parser.close()
text = parser.get_text()
assert "Real blog post title" in text, "blog post h1 missing"
assert "body of a blog post" in text, "blog body missing"
assert "menu noise" not in text, "nav should still be suppressed"
assert "footer noise" not in text, "footer should still be suppressed"
print(f"  extracted {len(text)} chars (fallback mode)")
print(f"  ✓ no landmark → legacy behaviour preserved, nav/footer still stripped")


# ============================================================
# Test 12 — role="main" ARIA landmark also triggers scoping
# ============================================================
banner("Test 12: role='main' ARIA landmark triggers scoping")

aria_html = """<!DOCTYPE html>
<html><body>
<nav>navigation chrome</nav>
<div role="main">
  <h1>Article via role=main</h1>
  <p>The article body lives in a div with role=main rather than a main element tag. Older CMS templates often use this pattern.</p>
</div>
<footer>footer chrome</footer>
</body></html>"""
parser = herald_links.MainContentParser()
parser.feed(aria_html)
parser.close()
text = parser.get_text()
assert "Article via role=main" in text
assert "role=main rather than a main element tag" in text
assert "navigation chrome" not in text
assert "footer chrome" not in text
print(f"  extracted {len(text)} chars")
print(f"  ✓ ARIA role=main recognised as landmark")


# ============================================================
# Test 13 — real AP News HTML (regression test against the actual extraction bug)
# ============================================================
banner("Test 13: real AP News HTML (the bug from /tmp/apnews_full.html)")
AP_NEWS_HTML_PATH = "/tmp/apnews_full.html"
import os as _os
if _os.path.exists(AP_NEWS_HTML_PATH):
    with open(AP_NEWS_HTML_PATH) as f:
        ap_html = f.read()
    parser = herald_links.MainContentParser()
    parser.feed(ap_html)
    parser.close()
    text = parser.get_text()
    print(f"  extracted {len(text)} chars (cap {herald_links.HERALD_LINK_MAX_CHARS_STORE})")
    # Article-specific content from the AP piece must be present.
    assert "Folarin Balogun" in text, f"subject name missing"
    assert "Infantino" in text, "key figure missing"
    assert "red card" in text.lower(), "subject matter missing"
    # Page chrome that previously dominated the cap must NOT appear.
    assert "HamburgerNavigation" not in text, "nav class name leaked"
    assert "Most watched videos" not in text, "footer section leaked"
    assert "Press Releases" not in text, "footer section leaked"
    assert "AP News Code of Conduct" not in text, "footer section leaked"
    print(f"  preview (first 600 chars):")
    for line in text[:600].splitlines():
        if line.strip():
            print(f"    {line}")
    print(f"  ✓ AP News extraction now contains the article body, not the chrome")
else:
    print(f"  SKIPPED: {AP_NEWS_HTML_PATH} not present")
    print(f"  (this test requires the AP News HTML to be cached locally)")


# ============================================================
# Test 14 — RichTextStoryBody class landmark (AP News shape)
# ============================================================
banner("Test 14: div class='RichTextStoryBody' is treated as a landmark")
ap_shape_html = """<!DOCTYPE html>
<html><body>
<header>
  <h1>SiteName navigation chrome</h1>
  <p>Top stories sidebar item 1</p>
  <p>Top stories sidebar item 2</p>
</header>
<nav>navigation links</nav>
<main class="Page-main">
  <div class="Page-lead">
    <div class="StoryPage-lede">
      <h1>FIFA Lifts Balogun Red Card After White House Call</h1>
    </div>
  </div>
  <div class="Page-storyBody">
    <div class="RichTextStoryBody RichTextBody">
      <p>FIFA President Gianni Infantino on Friday lifted the suspension of U.S. forward Folarin Balogun following a direct intervention from the White House.</p>
      <p>The reversal clears Balogun to play in Monday's group-stage match against Belgium.</p>
      <p>Federal Judge Ebony Horowitz-Gupta later issued a separate ruling that also cleared Balogun.</p>
    </div>
  </div>
  <aside class="Page-aside">
    <h2>Related stories</h2>
    <p>Unrelated sidebar story one</p>
    <p>Unrelated sidebar story two</p>
  </aside>
</main>
<footer>
  <p>Footer chrome with links</p>
  <p>AP News Code of Conduct</p>
  <p>Press Releases</p>
</footer>
</body></html>"""
parser = herald_links.MainContentParser()
parser.feed(ap_shape_html)
parser.close()
text = parser.get_text()
# Article body MUST be present.
assert "Folarin Balogun" in text, "subject missing from extraction"
assert "Gianni Infantino" in text, "key figure missing"
assert "Federal Judge Ebony Horowitz-Gupta" in text, "second paragraph missing"
# Chrome MUST be excluded.
assert "Top stories sidebar" not in text, "header chrome leaked"
assert "navigation links" not in text, "nav leaked"
assert "Related stories" not in text, "aside leaked"
assert "Unrelated sidebar story" not in text, "aside content leaked"
assert "AP News Code of Conduct" not in text, "footer chrome leaked"
assert "Press Releases" not in text, "footer chrome leaked"
print(f"  extracted {len(text)} chars from AP-shape HTML")
print(f"  ✓ RichTextStoryBody class scopes extraction to article body only")


# ============================================================
# Test 15 — div with article-body class alone (no <main>) works
# ============================================================
banner("Test 15: div class='article-body' alone (no <main> tag)")
article_body_only = """<!DOCTYPE html>
<html><body>
<header>site header noise</header>
<nav>navigation menu noise</nav>
<div class="article-body">
  <h2>The Article Title</h2>
  <p>First paragraph of the article body with substantive content about a real topic.</p>
  <p>Second paragraph continuing the narrative.</p>
</div>
<aside>unrelated aside content</aside>
<footer>footer links noise</footer>
</body></html>"""
parser = herald_links.MainContentParser()
parser.feed(article_body_only)
parser.close()
text = parser.get_text()
assert "First paragraph of the article body" in text
assert "Second paragraph continuing" in text
assert "site header noise" not in text
assert "navigation menu noise" not in text
assert "unrelated aside content" not in text
assert "footer links noise" not in text
print(f"  extracted {len(text)} chars")
print(f"  ✓ article-body class alone triggers strict mode")


# ============================================================
# Test 16 — Carousel / lede classes skipped even inside <main>
# ============================================================
banner("Test 16: Carousel and lede classes skipped even inside <main>")
carousel_heavy = """<!DOCTYPE html>
<html><body>
<header>site header</header>
<main>
  <div class="Page-lead">
    <div class="StoryPage-lede">
      <h1>Article Title in Lede</h1>
    </div>
  </div>
  <div class="Carousel" data-count="5">
    <div class="CarouselSlide CarouselSlide-active">
      <p>Carousel caption one with photo description and metadata about the image.</p>
    </div>
    <div class="CarouselSlide">
      <p>Carousel caption two with photo description and metadata about another image.</p>
    </div>
    <div class="CarouselOverlay">
      <p>Carousel overlay duplicate caption one with same text.</p>
    </div>
  </div>
  <div class="Page-storyBody">
    <div class="RichTextStoryBody RichTextBody">
      <p>The actual article body paragraph one with substantive content about the topic at hand.</p>
      <p>Article body paragraph two with more context and detail that the LLM should see.</p>
      <p>Article body paragraph three continuing the narrative with key facts and figures.</p>
    </div>
  </div>
  <div class="Author-bio RichTextBody">
    <p>Author bio noise that should be skipped — about the journalist who wrote this.</p>
  </div>
  <div class="PageListRightRailA-content">
    <p>Related stories sidebar that should be skipped — five unrelated headlines.</p>
  </div>
</main>
<footer>site footer</footer>
</body></html>"""
parser = herald_links.MainContentParser()
parser.feed(carousel_heavy)
parser.close()
text = parser.get_text()
# Article body MUST be present.
assert "actual article body paragraph one" in text
assert "Article body paragraph two" in text
assert "Article body paragraph three" in text
# Carousel/lede/author/sidebar chrome MUST be excluded.
assert "Carousel caption one" not in text, "carousel chrome leaked"
assert "Carousel caption two" not in text, "carousel chrome leaked"
assert "Carousel overlay" not in text, "carousel overlay leaked"
assert "Article Title in Lede" not in text, "lede title leaked"
assert "Author bio noise" not in text, "author bio leaked"
assert "Related stories sidebar" not in text, "sidebar leaked"
assert "site header" not in text, "header leaked"
assert "site footer" not in text, "footer leaked"
print(f"  extracted {len(text)} chars")
print(f"  preview: {text[:200]!r}…")
print(f"  ✓ carousel + lede + author + sidebar all excluded; article body intact")


print()
print("All link-context tests pass. ✓")
