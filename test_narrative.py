"""Offline checks for Narrative v2's bounded story output."""
import os
import tempfile
import time


with tempfile.TemporaryDirectory() as tmp:
    os.environ["HERALD_DB_PATH"] = f"{tmp}/narrative.db"
    import herald_db
    import herald_narrative

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    now = int(time.time())
    post_ids = []
    for i in range(10):
        pid = str(5000 + i)
        herald_db.upsert_post(conn, {
            "id": pid, "text": f"timeline source {i}",
            "author": {"id": str(i), "username": f"source{i}"},
            "metrics": {"likes": i, "retweets": 0, "replies": 0},
            "conversation": {"id": pid, "parent_id": None}, "quoted_tweet": None,
            "media": None, "links": None,
            "created_at": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime(now)),
            "post_type": "original",
        })
        post_ids.append(pid)
    herald_db.record_timeline_capture(conn, post_ids, captured_at=now)
    conn.commit()
    issue = herald_narrative.build_issue(conn, now - 60, now + 1, dry_run=True)
    assert issue["candidate_count"] == 10
    assert len(issue["stories"]) == 7
    assert all("[post:" in story["story"] for story in issue["stories"])
    assert issue["evidence"]["context_reads"] == 0

    # Writer entries are matched by stable pack order, not a model-rewritten
    # label. This prevents a valid narrative response falling into the
    # extractive fallback solely because it paraphrased a theme name.
    plan = {"stories": [{"label": "one", "angle": "", "kind": "sidebar", "post_ids": ["5000"]}]}
    post = herald_narrative._timeline_shape(dict(conn.execute("SELECT * FROM posts WHERE external_id='5000'").fetchone()))
    original_call = herald_narrative.herald_synthesise._call
    herald_narrative.herald_synthesise._call = lambda *a, **k: (
        '{"stories":[{"story_index":1,"headline":"rewritten label","story":"A readable take [post:5000]","topic":"sidebar","key_quotes":[]}]}',
        {}, "test", "stop")
    try:
        written, _usage, _model, _finish = herald_narrative._write_issue(plan, {"5000": post})
    finally:
        herald_narrative.herald_synthesise._call = original_call
    assert written[0]["headline"] == "rewritten label"
    conn.close()

print("narrative tests pass")
