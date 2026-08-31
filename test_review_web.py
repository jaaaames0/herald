"""Offline access-control and rendering test for the private review desk."""
import base64
import importlib.util
import os
import tempfile
import time


if importlib.util.find_spec("flask") is None:
    print("review web tests skipped (flask unavailable in this interpreter)")
else:
 with tempfile.TemporaryDirectory() as tmp:
  os.environ["HERALD_DB_PATH"] = f"{tmp}/review.db"
  os.environ["HERALD_REVIEW_PASSWORD"] = "test-review-password"
  import herald_db
  import herald_web

  conn = herald_db.get_connection()
  herald_db.init_schema(conn)
  now = int(time.time())
  herald_db.upsert_post(conn, {
        "id": "review-lead", "text": "A self-contained review story.",
        "author": {"id": "author", "username": "author"},
        "metrics": {}, "media": None, "links": None,
        "conversation": {"id": "review-lead", "parent_id": None},
        "quoted_tweet": None,
        "created_at": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime(now)),
        "post_type": "original",
  })
  candidate = {
        "anchor_key": "lead:review-lead", "anchor": "Review story",
        "story_type": "one_shot", "state": "ready", "urgency": "normal",
        "readiness": 1.0, "rationale": "A test story.",
        "sources": [("review-lead", "lead", 1.0)],
  }
  candidate_id = herald_db.upsert_story_candidate(conn, candidate, candidate["sources"], model="test")
  herald_db.save_story_candidate_draft(conn, candidate_id, {
        "headline": "Private headline", "story": "Private prose [post:review-lead]",
        "model_version": "test", "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0,
  })
  issue_id = herald_db.create_story_issue(conn, candidate_ids=[candidate_id], title="Private issue")
  conn.commit()
  conn.close()

  client = herald_web.app.test_client()
  assert client.get("/desk").status_code == 401
  auth = base64.b64encode(b"herald:test-review-password").decode()
  headers = {"Authorization": f"Basic {auth}"}
  desk = client.get("/desk", headers=headers)
  assert desk.status_code == 200 and b"Private issue" in desk.data
  issue = client.get(f"/desk/issue/{issue_id}", headers=headers)
  assert issue.status_code == 200 and b"Private headline" in issue.data
  conn = herald_db.get_connection()
  herald_db.publish_story_issue(conn, issue_id)
  conn.commit()
  conn.close()
  public = client.get(f"/issue/{issue_id}")
  assert public.status_code == 200 and b"Private headline" in public.data

  print("review web tests pass")
