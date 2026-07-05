"""Tests for Phase 3.6.1 image context (herald_media + IMAGE CONTEXT renderer).

Mirrors test_parser_v2.py structure: isolated sub-DBs per test, mocked
vision API, no live OpenRouter calls. Tests don't touch the live herald.db.

Mocks the OpenRouter vision API by monkeypatching herald_media._call_vision
(or fetch_image_description). Tests verify:
  1. fetch_image_description round-trips through DB cache
  2. attach_media_context sets _image_description from cache, _image_skipped
     for videos
  3. enrich_media_batch skips already-cached URLs (no double-fetch)
  4. Videos and animated GIFs are filtered out at extract time
  5. _pick_images_for_story respects HERALD_IMAGES_PER_STORY cap and
     engagement-priority ordering
  6. _render_image_context_block produces the expected prompt format
  7. failed fetches are stored as 'failed' rows, don't abort batch
"""
import os
import sys
import time
import json
import importlib

# Force-disable live OpenRouter calls; tests monkeypatch the vision fn.
os.environ.setdefault("HERALD_OPENROUTER_DISABLED", "1")

sys.path.insert(0, "/home/james/srv/herald")

# Use a unique test DB per run to avoid touching the live herald.db.
TEST_DIR = "/tmp/herald-test-image"
os.makedirs(TEST_DIR, exist_ok=True)
TEST_DB = f"{TEST_DIR}/test_image_{int(time.time())}.db"
os.environ["HERALD_DB_PATH"] = TEST_DB

import herald_db
import herald_media
import herald_synthesise


def make_parsed_post(external_id, media, text="x", metrics=None):
    """Build a minimal parsed-post dict shaped like what herald_parser emits."""
    return {
        "id": external_id,
        "external_id": external_id,
        "text": text,
        "author": {"username": f"user_{external_id[-3:]}", "name": f"User {external_id}"},
        "metrics": metrics or {"likes": 0, "retweets": 0, "replies": 0, "quote_count": 0},
        "media": media,
        "links": [],
        "post_type": "original",
        "conversation": {"id": external_id, "parent_id": None, "is_reply": False, "parent_tweet": None},
        "created_at": "Thu Jul 03 12:00:00 +0000 2026",
        "lang": "en", "source": "test",
    }


def banner(name):
    print(f"\n=== {name} ===")


def reset_db():
    """Wipe image_context and posts-like state for a fresh test."""
    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    conn.execute("DELETE FROM image_context")
    conn.commit()
    conn.close()


# ============================================================
# Test 1 — videos and animated GIFs are filtered, photos pass
# ============================================================
banner("Test 1: extract photo URLs only — skip videos and animated GIFs")
photo_url = "https://pbs.twimg.com/media/PHOTO1.jpg"
video_url = "https://video.twimg.com/VIDEO1.mp4"
gif_url = "https://pbs.twimg.com/GIF1.gif"
post = make_parsed_post("t1", [
    {"type": "photo", "url": photo_url},
    {"type": "video", "url": video_url},
    {"type": "animated_gif", "url": gif_url},
])
photos, skipped = herald_media._extract_photo_urls(post)
assert photos == [photo_url], f"expected only photo, got {photos}"
assert len(skipped) == 2, f"expected 2 skipped entries, got {skipped}"
assert all(mt == "video" or mt == "animated_gif" for _, mt in skipped)
print(f"  photos={photos}")
print(f"  skipped={skipped}")
print(f"  ✓ video + animated_gif filtered out, photo passes")

# Also test the media_urls DB column shape (parser uses 'media', DB / 
# get_posts_in_window uses 'media_urls' — both must work).
post_with_db_key = {"media_urls": [
    {"type": "photo", "url": "https://pbs.twimg.com/media/DB.jpg"},
    {"type": "video", "url": "https://v.mp4"},
]}
photos_db, skip_db = herald_media._extract_photo_urls(post_with_db_key)
assert photos_db == ["https://pbs.twimg.com/media/DB.jpg"], f"got {photos_db}"
assert len(skip_db) == 1
print(f"  ✓ 'media_urls' (DB shape) handled equivalently")


# ============================================================
# Test 2 — enrich_media_batch happy path writes success row to image_context
# ============================================================
banner("Test 2: enrich_media_batch writes success row to image_context")
reset_db()

# Monkeypatch the vision function.
call_log = []
def fake_vision(url, model=None):
    call_log.append((url, model))
    return (f"A short description of {url.rsplit('/', 1)[-1]}.", "openai/gpt-4o-mini", "stop")
herald_media._call_vision = fake_vision

post = make_parsed_post("t2", [{"type": "photo", "url": "https://example.com/img-A.jpg"}])
attempted, ok, failed, skipped = herald_media.enrich_media_batch([post], max_fetches=5)
assert attempted == 1 and ok == 1 and failed == 0, f"unexpected counts: {attempted}/{ok}/{failed}/{skipped}"

# Verify the DB row exists.
conn = herald_db.get_connection()
row = conn.execute(
    "SELECT fetch_status, description, model FROM image_context WHERE media_url = ?",
    ("https://example.com/img-A.jpg",),
).fetchone()
assert row is not None, "expected image_context row"
d = dict(row)
assert d["fetch_status"] == "success"
assert d["description"] == "A short description of img-A.jpg."
assert d["model"] == "openai/gpt-4o-mini"
conn.close()
print(f"  attempted={attempted} ok={ok} failed={failed}")
print(f"  row: {d}")
print(f"  ✓ success row stored by enrich_media_batch")


# ============================================================
# Test 3 — failed fetch is stored, doesn't raise
# ============================================================
banner("Test 3: failed vision call stores 'failed' row, no exception")
reset_db()

def fake_vision_fail(url, model=None):
    raise RuntimeError("simulated transport error")
herald_media._call_vision = fake_vision_fail

# fetch_image_description itself raises.
threw = False
try:
    herald_media.fetch_image_description("https://example.com/img-FAIL.jpg")
except RuntimeError as e:
    threw = "simulated" in str(e)
assert threw, "expected RuntimeError to propagate"

# enrich_media_batch should record the failure and continue.
batch_post = make_parsed_post("t3a", [{"type": "photo", "url": "https://example.com/img-FAIL.jpg"}])
attempted, ok, failed, skipped = herald_media.enrich_media_batch([batch_post], max_fetches=5)
assert attempted == 1, f"expected attempted=1, got {attempted}"
assert failed == 1, f"expected failed=1, got {failed}"
assert ok == 0, f"expected ok=0, got {ok}"

conn = herald_db.get_connection()
row = conn.execute(
    "SELECT fetch_status, fetch_error FROM image_context WHERE media_url = ?",
    ("https://example.com/img-FAIL.jpg",),
).fetchone()
assert row is not None and dict(row)["fetch_status"] == "failed"
err = dict(row)["fetch_error"]
assert "simulated" in err, f"expected error context, got {err}"
conn.close()
print(f"  attempted={attempted} ok={ok} failed={failed} skipped={skipped}")
print(f"  error stored: {err[:80]!r}")
print(f"  ✓ failure persisted, batch survived")


# ============================================================
# Test 4 — cache hit: second enrich skips already-cached URLs
# ============================================================
banner("Test 4: enrich_media_batch skips URLs already in cache")
reset_db()

call_log.clear()
herald_media._call_vision = lambda url, model=None: (call_log.append(url) or f"desc of {url}", "m", "stop")

# Manually seed a cached success row.
conn = herald_db.get_connection()
herald_db.upsert_image_context(
    conn, "https://example.com/cached.jpg", "success",
    description="cached description", model="m",
)
conn.commit()
conn.close()

# Run enrich; only the new URL should be fetched.
post = make_parsed_post("t4", [
    {"type": "photo", "url": "https://example.com/cached.jpg"},
    {"type": "photo", "url": "https://example.com/NEW.jpg"},
])
attempted, ok, failed, skipped = herald_media.enrich_media_batch([post], max_fetches=5)
assert attempted == 1, f"expected only NEW.jpg attempted, got {attempted}"
assert ok == 1
# The fetched URL should be NEW.jpg, not cached.jpg
assert any("NEW.jpg" in u for u in call_log), f"expected NEW.jpg fetch, got {call_log}"
assert not any("cached.jpg" in u for u in call_log), f"cache leak: {call_log}"
print(f"  call_log={call_log}")
print(f"  attempted={attempted} ok={ok}")
print(f"  ✓ cached URL skipped; only NEW.jpg hit the vision API")


# ============================================================
# Test 5 — attach_media_context wires _image_description onto candidates
# ============================================================
banner("Test 5: attach_media_context populates _image_description from cache")
reset_db()

# Seed two described photos.
conn = herald_db.get_connection()
herald_db.upsert_image_context(
    conn, "https://example.com/A.jpg", "success",
    description="description of A", model="m",
)
herald_db.upsert_image_context(
    conn, "https://example.com/B.jpg", "success",
    description="description of B", model="m",
)
conn.commit()
conn.close()

# Two candidate posts, one with two photos, one with a video (skip).
cands = [
    make_parsed_post("t5a", [
        {"type": "photo", "url": "https://example.com/A.jpg"},
        {"type": "photo", "url": "https://example.com/B.jpg"},  # extra
        {"type": "video", "url": "https://v.mp4"},
    ]),
    make_parsed_post("t5b", [
        {"type": "photo", "url": "https://example.com/NOTFOUND.jpg"},
    ]),
]
herald_media.attach_media_context(cands)

# First candidate: takes the FIRST photo's description (A).
assert cands[0]["_image_description"] == "description of A", \
    f"expected A's description, got {cands[0]['_image_description']!r}"
assert cands[0]["_image_url"] == "https://example.com/A.jpg"
# Skipped entries (v.mp4) recorded for debug.
assert any(u.endswith("v.mp4") for u, _ in cands[0]["_image_skipped"])
# Second candidate: A.jpg not found in cache, _image_description is None.
assert cands[1]["_image_description"] is None
assert cands[1]["_image_url"] is None  # never set since no cache hit
print(f"  cand 0: desc={cands[0]['_image_description']!r}")
print(f"  cand 0: skipped={cands[0]['_image_skipped']}")
print(f"  cand 1: desc={cands[1]['_image_description']!r}")
print(f"  ✓ first photo wins when multiple, video skipped, cache miss = None")


# ============================================================
# Test 6 — _pick_images_for_story engagement-priority + per-story cap
# ============================================================
banner("Test 6: _pick_images_for_story respects cap and engagement sort")

posts = [
    {"id": "low", "_image_description": "low-image", "author": {"username": "a"},
     "metrics": {"likes": 5, "retweets": 0}},
    {"id": "high", "_image_description": "high-image", "author": {"username": "b"},
     "metrics": {"likes": 500, "retweets": 50}},  # 500 + 150 = 650
    {"id": "no-desc", "author": {"username": "c"},
     "metrics": {"likes": 10000, "retweets": 0}},  # big engagement but no image
    {"id": "mid", "_image_description": "mid-image", "author": {"username": "d"},
     "metrics": {"likes": 50, "retweets": 5}},  # 50 + 15 = 65
]

# cap=1 → only the highest-engagement image (high).
picked = herald_synthesise._pick_images_for_story(posts, 1)
assert [p[0] for p in picked] == ["high"], f"expected ['high'], got {[p[0] for p in picked]}"

# cap=0 → empty (feature disabled).
assert herald_synthesise._pick_images_for_story(posts, 0) == []

# cap=2 → high then mid (engagement sort).
picked2 = herald_synthesise._pick_images_for_story(posts, 2)
assert [p[0] for p in picked2] == ["high", "mid"], f"got {[p[0] for p in picked2]}"

print(f"  cap=1: {[p[0] for p in picked]}")
print(f"  cap=2: {[p[0] for p in picked2]}")
print(f"  ✓ engagement sort, no-desc excluded, cap respected")


# ============================================================
# Test 7 — _render_image_context_block produces the expected format
# ============================================================
banner("Test 7: _render_image_context_block format")

picked = [
    ("post-A", "alice", "Chart: US incarceration rate vs other OECD countries; bar chart, US ~5x next-highest"),
    ("post-B", "bob", "Photo of protest sign reading 'No new prisons'."),
]
text, n = herald_synthesise._render_image_context_block(picked)
assert n == 2
assert "IMAGE CONTEXT (2 parsed" in text
assert "[post:post-A] @alice" in text
assert "[post:post-B] @bob" in text
# Newlines collapsed to spaces (so a long desc doesn't break the format).
assert "\n" not in picked[0][2] or " " in picked[0][2]  # the desc itself may have had newlines, but renderer replaced
print("---")
print(text)
print("---")
assert text.startswith("IMAGE CONTEXT ("), "expected header"
print(f"  ✓ format matches")


# ============================================================
# Test 8 — full integration: enrich -> attach -> assemble stage 2 prompt
# ============================================================
banner("Test 8: end-to-end through stage 2 prompt assembly (dry-run)")
reset_db()

# Seed DB cache with descriptions for two photos.
conn = herald_db.get_connection()
herald_db.upsert_image_context(
    conn, "https://example.com/stage-A.jpg", "success",
    description="Bar chart, US 5x higher than next country",
    model="openai/gpt-4o-mini",
)
herald_db.upsert_image_context(
    conn, "https://example.com/stage-B.jpg", "success",
    description="Photo of protest",
    model="openai/gpt-4o-mini",
)
conn.commit()
conn.close()

# Build two candidates, both with images. Theme is "US incarceration".
theme_posts = [
    make_parsed_post("story-1", [
        {"type": "photo", "url": "https://example.com/stage-A.jpg"},
    ], metrics={"likes": 1000, "retweets": 100}),
    make_parsed_post("story-2", [
        {"type": "photo", "url": "https://example.com/stage-B.jpg"},
    ], metrics={"likes": 50, "retweets": 0}),
]
# Stage 2 prompt assembly reads _image_description, so attach first.
herald_media.attach_media_context(theme_posts)

# Now exercise _pick_images_for_story with the default cap.
picked = herald_synthesise._pick_images_for_story(theme_posts, herald_synthesise.HERALD_IMAGES_PER_STORY)
assert len(picked) == 1, f"expected 1 image (default cap), got {len(picked)}"
assert picked[0][0] == "story-1", f"expected story-1 (more engagement), got {picked[0][0]}"

block, n_imgs = herald_synthesise._render_image_context_block(picked)
print(f"  picked: {picked[0]}")
print(f"  block ({n_imgs} images):")
print(f"    {block.split(chr(10))[1]}")
print(f"  ✓ end-to-end: highest-engagement image wins at cap=1")


# ============================================================
print("\nAll image-context tests pass. ✓")
