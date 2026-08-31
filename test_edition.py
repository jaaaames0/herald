"""Focused checks for the bounded timeline-edition path."""
import os
import tempfile
import time


with tempfile.TemporaryDirectory() as tmp:
    os.environ["HERALD_DB_PATH"] = f"{tmp}/edition.db"
    import herald_db
    import herald_edition

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    now = int(time.time())
    ids = []
    for i in range(30):
        post = {
            "id": str(1000 + i), "text": f"post {i}",
            "author": {"id": str(i), "username": f"author{i}"},
            "metrics": {"likes": i, "retweets": 0, "replies": 0},
            "conversation": {"id": str(1000 + i), "parent_id": None},
            "quoted_tweet": None, "media": None, "links": None,
            "created_at": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime(now)),
            "post_type": "original",
        }
        herald_db.upsert_post(conn, post)
        ids.append(post["id"])
    capture_id = herald_db.record_timeline_capture(conn, ids, captured_at=now)
    conn.commit()
    assert capture_id

    edition = herald_edition.build_edition(conn, now - 60, now + 1, dry_run=True)
    assert edition["ledger"]["captured"] == 30
    assert edition["ledger"]["unique"] == 30
    assert edition["ledger"]["shown"] == 22
    assert edition["ledger"]["collapsed_or_omitted"] == 8
    assert edition["ledger"]["context_reads"] == 0
    assert [card["external_id"] for card in edition["cards"]][:2] == ["1000", "1001"]

    # Context is a hard per-edition budget, even when every visible card is
    # an opaque reply.  Mock the network helper rather than touching X.
    conn.execute("UPDATE posts SET post_type='reply', in_reply_to_id='parent'")
    calls = []
    original_fetch = herald_edition._fetch_context
    def fake_context(post, budget):
        if budget[0] <= 0:
            return []
        budget[0] -= 1
        calls.append(post["id"])
        return []
    herald_edition._fetch_context = fake_context
    try:
        bounded = herald_edition.build_edition(conn, now - 60, now + 1, dry_run=True)
    finally:
        herald_edition._fetch_context = original_fetch
    assert len(calls) == 12
    assert bounded["ledger"]["context_reads"] == 12
    conn.close()

print("edition tests pass")
