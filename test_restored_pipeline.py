"""Offline checks for the restored editor and its targeted evidence boundary."""
import argparse
import os
import tempfile
import time


with tempfile.TemporaryDirectory() as tmp:
    os.environ["HERALD_DB_PATH"] = f"{tmp}/restore.db"
    import herald_db
    import herald_digest
    import herald_evidence

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    # Keep source timestamps safely inside ``--since 1h ago``'s exclusive
    # right edge rather than exactly equal to the command's current second.
    now = int(time.time()) - 5
    for pid, text in (("100", "Kimi K3 has arrived"), ("101", "Developers are testing Kimi K3")):
        herald_db.upsert_post(conn, {
            "id": pid, "text": text, "author": {"id": pid, "username": f"source{pid}"},
            "metrics": {"likes": 10, "retweets": 2, "replies": 1}, "media": None, "links": None,
            "conversation": {"id": pid, "parent_id": None}, "quoted_tweet": None,
            "created_at": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime(now)),
            "post_type": "original",
        })
    conn.commit()
    conn.close()

    original_select = herald_digest.herald_embed.select_candidates
    original_stage1 = herald_digest.herald_synthesise.stage1_timeline_map
    herald_digest.herald_embed.select_candidates = lambda posts, **_kwargs: ([0, 1], [], {"prompt_tokens": 0, "cost": 0})
    herald_digest.herald_synthesise.stage1_timeline_map = lambda *_args, **_kwargs: ({
        "zeitgeist": [{"label": "Kimi", "post_ids": ["100", "101"], "accounts": [], "salience": "high"}],
        "stories_to_write": [{"label": "Kimi", "angle": "A model launch."}], "items": [], "editor_notes": "",
    }, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}, "test", "stop")
    try:
        result = herald_digest.cmd_digest(argparse.Namespace(since="1h ago", top=7, max_candidates=40, dry_run=True, show=False))
    finally:
        herald_digest.herald_embed.select_candidates = original_select
        herald_digest.herald_synthesise.stage1_timeline_map = original_stage1
    assert result == 0

    conn = herald_db.get_connection()
    digest_id = conn.execute("SELECT id FROM digests ORDER BY id DESC LIMIT 1").fetchone()["id"]
    dossier = herald_db.get_digest_dossier(conn, digest_id)
    conn.close()
    assert dossier["pipeline"] == "legacy-editor-targeted-evidence-v1"
    assert dossier["stories"][0]["evidence"]["context_reads"] == 0

    # A selected quote follows only its explicit quote chain, never a thread
    # crawl. This is essential when the visible quote is commentary on a
    # quote-of-a-quote; the writer needs the original proposition as well.
    post = {"id": "200", "quoted_tweet_id": "parent", "in_reply_to_id": None,
            "text": "what does this mean?", "metrics": {}, "link_urls": [], "media": []}
    original_cached = herald_evidence._cached_parent
    original_inline = herald_evidence._cached_parent_is_inline
    original_store = herald_evidence._store_parent
    original_fetch = herald_evidence.herald_context.fetch_parent_raw
    original_parse = herald_evidence.herald_context.herald_parser.parse_tweet
    calls = []
    herald_evidence._cached_parent = lambda _target: None
    herald_evidence._cached_parent_is_inline = lambda _target: False
    herald_evidence._store_parent = lambda *_args, **_kwargs: None
    herald_evidence.herald_context.fetch_parent_raw = lambda target: calls.append(target) or {"id": target}
    def parse_parent(raw):
        result = {"id": raw["id"], "text": f"context for {raw['id']}",
                  "author": {"username": "parent"}}
        if raw["id"] == "parent":
            result["quoted_tweet"] = {"id": "grandparent"}
        return result
    herald_evidence.herald_context.herald_parser.parse_tweet = parse_parent
    try:
        evidence = herald_evidence.enrich_story_posts([post], {"context": 2, "links": 0, "images": 0})
    finally:
        herald_evidence._cached_parent = original_cached
        herald_evidence._cached_parent_is_inline = original_inline
        herald_evidence._store_parent = original_store
        herald_evidence.herald_context.fetch_parent_raw = original_fetch
        herald_evidence.herald_context.herald_parser.parse_tweet = original_parse
    assert calls == ["parent", "grandparent"]
    assert evidence["context_reads"] == 2
    assert [item["tweet"]["id"] for item in post["_quote_context"]] == ["parent", "grandparent"]

    # An explicit thread marker earns at most one remote read, and the result
    # is filtered to self-replies before it enters the evidence packet.
    marker = {
        "id": "thread-1", "text": "Here is the explanation 🧵", "metrics": {},
        "author": {"id": "author-1", "username": "author"},
        "conversation": {"id": "thread-1"}, "link_urls": [], "media": [],
    }
    original_thread = herald_evidence.herald_context.fetch_thread_raw
    original_parse = herald_evidence.herald_context.herald_parser.parse_tweet
    thread_calls = []
    herald_evidence.herald_context.fetch_thread_raw = lambda cid: thread_calls.append(cid) or [
        {"id": "thread-1"}, {"id": "self-2"}, {"id": "other-1"}, {"id": "off-chain"},
    ]
    herald_evidence.herald_context.herald_parser.parse_tweet = lambda raw: {
        "id": raw["id"], "text": raw["id"], "created_at": raw["id"],
        "author": {"id": "author-1" if raw["id"] != "other-1" else "other", "username": "x"},
        "conversation": {"parent_id": "thread-1" if raw["id"] == "self-2" else "other-1"},
    }
    try:
        thread_evidence = herald_evidence.enrich_story_posts(
            [marker], {"context": 0, "links": 0, "images": 0, "threads": 1},
        )
    finally:
        herald_evidence.herald_context.fetch_thread_raw = original_thread
        herald_evidence.herald_context.herald_parser.parse_tweet = original_parse
    assert thread_calls == ["thread-1"]
    assert [item["id"] for item in marker["_thread_context"]] == ["self-2"]
    assert thread_evidence["thread_remote_success"] == 1

print("restored pipeline tests pass")
