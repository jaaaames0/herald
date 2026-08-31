"""Offline tests for persistent story-desk state and metric observations."""
import os
import tempfile
import time


with tempfile.TemporaryDirectory() as tmp:
    os.environ["HERALD_DB_PATH"] = f"{tmp}/desk.db"
    import herald_db
    import herald_desk

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    now = int(time.time())
    for pid, text in (("lead", "A claim about mathematics"), ("reply", "A response to that claim"),
                      ("later", "A later development about mathematics")):
        herald_db.upsert_post(conn, {
            "id": pid, "text": text, "author": {"id": pid, "username": pid},
            "metrics": {"likes": 5, "retweets": 2, "replies": 1}, "media": None, "links": None,
            "conversation": {"id": pid, "parent_id": None}, "quoted_tweet": None,
            "created_at": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime(now)),
            "post_type": "original",
        })
    capture_id = herald_db.record_timeline_capture(conn, ["lead", "reply"], captured_at=now)
    observation = conn.execute("SELECT metrics FROM post_observations WHERE capture_id=?", (capture_id,)).fetchone()
    assert observation is not None

    chosen = herald_desk._normalise({"candidates": [{
        "lead_post_id": "lead", "anchor": "What AI proofs mean for mathematics",
        "story_type": "timeline_argument", "state": "watching", "urgency": "developing",
        "readiness": 0.4, "rationale": "Needs the quoted parent and a counterpoint.",
        "sources": [{"post_id": "lead", "role": "lead"}, {"post_id": "reply", "role": "counterpoint"}],
    }]}, {"lead", "reply"})
    assert len(chosen) == 1
    # An unrelated high-engagement joke cannot become a story's foundation.
    guarded = herald_desk._normalise({"candidates": [{
        "lead_post_id": "joke", "anchor": "LLMs solve open mathematics conjectures",
        "sources": [{"post_id": "joke", "role": "lead"}, {"post_id": "lead", "role": "lead"}],
    }]}, {"joke", "lead"}, {"joke": "When my wife rejects dinner suggestions", "lead": "LLMs solve open mathematics conjectures"})
    assert len(guarded) == 1 and guarded[0]["anchor_key"] == "lead:lead"
    # Frontier models commonly honour JSON-only requests but still wrap the
    # payload in a Markdown fence. The desk must preserve valid work.
    fenced = herald_desk._parse_response('```json\n{"candidates": []}\n```')
    assert fenced == {"candidates": []}
    candidate_id = herald_db.upsert_story_candidate(conn, chosen[0], chosen[0]["sources"], model="test")
    # A later review of the same lead enriches the existing candidate.
    herald_db.upsert_story_candidate(conn, chosen[0], chosen[0]["sources"], model="test")
    rows = herald_db.list_story_candidates(conn, state="watching")
    assert len(rows) == 1 and rows[0]["id"] == candidate_id and rows[0]["source_count"] == 2
    revised = dict(chosen[0])
    revised["anchor_key"] = "lead:reply"
    revised["sources"] = [("reply", "lead", 1.0)]
    herald_db.upsert_story_candidate(conn, revised, revised["sources"], model="test")
    assert herald_db.list_story_candidates(conn, state="watching")[0]["id"] == candidate_id
    sources = herald_db.get_story_candidate_posts(conn, candidate_id)
    assert [source["id"] for source in sources] == ["reply"]

    # A concrete later development can continue an unpublished candidate. The
    # new lead is current, while the previous lead stays as writer-visible
    # background instead of disappearing or becoming a duplicate story.
    later = {
        "anchor_key": "lead:later", "anchor": "A later mathematics development",
        "story_type": "timeline_argument", "state": "ready", "urgency": "developing",
        "readiness": 0.8, "rationale": "Adds a concrete later result.",
        "continuity_candidate_id": candidate_id,
        "continuity_note": "A new result extends the earlier debate.",
        "sources": [("later", "lead", 1.0)],
    }
    assert herald_db.upsert_story_candidate(conn, later, later["sources"], model="test") == candidate_id
    sources = herald_db.get_story_candidate_posts(conn, candidate_id)
    assert {source["id"]: source["desk_role"] for source in sources} == {
        "reply": "background", "later": "lead",
    }
    candidate = herald_db.list_story_candidates(conn, state="ready")[0]
    assert candidate["continuity_note"] == "A new result extends the earlier debate."

    # Once published, the old candidate is immutable. A later development is
    # a new candidate linked to that published history, not a naive re-run.
    conn.execute("UPDATE story_candidates SET state='published' WHERE id=?", (candidate_id,))
    followup = dict(later)
    followup["anchor_key"] = "lead:lead"
    followup["anchor"] = "Another later mathematics development"
    followup["sources"] = [("lead", "lead", 1.0)]
    followup_id = herald_db.upsert_story_candidate(conn, followup, followup["sources"], model="test")
    assert followup_id != candidate_id
    linked = conn.execute("SELECT continuity_parent_id FROM story_candidates WHERE id=?", (followup_id,)).fetchone()
    assert linked["continuity_parent_id"] == candidate_id

    # A single post can be a one-shot, but cannot be promoted as a reported
    # event merely because the planner called it ready.
    unsupported = dict(followup)
    unsupported["anchor_key"] = "lead:later"
    unsupported["anchor"] = "An unsupported reported event"
    unsupported["story_type"] = "reported_event"
    unsupported["continuity_candidate_id"] = None
    unsupported["sources"] = [("later", "lead", 1.0)]
    unsupported_id = herald_db.upsert_story_candidate(conn, unsupported, unsupported["sources"], model="test")
    verdict = herald_db.assess_story_candidate(conn, unsupported_id)
    stored = conn.execute("SELECT state FROM story_candidates WHERE id=?", (unsupported_id,)).fetchone()
    assert not verdict["ready"] and stored["state"] == "watching"

    herald_db.add_story_candidate_evidence(conn, candidate_id, "targeted_context", "test", "success", {"ok": True})
    assert conn.execute("SELECT count(*) FROM story_candidate_evidence").fetchone()[0] == 1
    packet = herald_desk.packet(conn, candidate_id)
    assert packet["candidate"]["id"] == candidate_id and len(packet["sources"]) == 2
    assert "Continuity" in herald_desk.render_packet_markdown(packet)
    draft_id, draft, usage, _model, finish = herald_desk.write_draft(conn, candidate_id, dry_run=True)
    assert draft_id and draft["headline"].startswith("[DRY-RUN]") and usage["cost"] == 0 and finish == "stop"
    # Issue assembly only selects ready candidates with an existing private
    # draft. It is a database-only editorial action, not publication.
    one_shot = dict(unsupported)
    one_shot["anchor_key"] = "lead:issue-one-shot"
    one_shot["anchor"] = "A self-contained one-shot"
    one_shot["story_type"] = "one_shot"
    one_shot["sources"] = [("lead", "lead", 1.0)]
    one_shot_id = herald_db.upsert_story_candidate(conn, one_shot, one_shot["sources"], model="test")
    herald_desk.write_draft(conn, one_shot_id, dry_run=True)
    issue_id = herald_db.create_story_issue(conn, candidate_ids=[one_shot_id], title="Test issue")
    issue = herald_db.get_story_issue(conn, issue_id)
    assert issue["state"] == "draft" and len(issue["items"]) == 1
    assert "private editorial draft" in herald_desk.render_issue_markdown(issue)
    published = herald_db.publish_story_issue(conn, issue_id)
    assert published["state"] == "published" and published["published_at"]
    assert herald_db.list_public_story_issues(conn)[0]["id"] == issue_id
    review_id = herald_db.save_story_desk_review(
        conn, now, now, ["lead"], '```json\n{"candidates": [{"lead_post_id":"lead", "anchor":"A claim about mathematics", "sources":[]}]}\n```', "test", "empty",
    )
    replayed, usage, _model, finish = herald_desk.replay_review(conn, review_id)
    assert len(replayed) == 1 and usage["cost"] == 0 and finish == "replay"
    conn.close()

print("story desk tests pass")
