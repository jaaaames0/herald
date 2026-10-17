"""Herald tests. No network: synthetic bird JSON, a fake LLM, a temp database.

    python3 -m unittest -v test_herald
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

TMP = tempfile.mkdtemp(prefix="herald-test-")
os.environ["HERALD_DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["HERALD_ENV_PATH"] = os.path.join(TMP, "missing.env")

import herald  # noqa: E402
import herald_edit  # noqa: E402
import herald_feed  # noqa: E402
import herald_llm  # noqa: E402
import herald_render_text  # noqa: E402
import herald_store  # noqa: E402
import herald_x  # noqa: E402

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def tw_time(hours_ago):
    return datetime.fromtimestamp(NOW.timestamp() - hours_ago * 3600, timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")


def result(pid, handle, text, hours_ago=1, likes=10, quoted=None, reply_to=None, reply_handle=None,
           media=None, card=None, urls=None, note=None):
    legacy = {"id_str": pid, "full_text": text, "created_at": tw_time(hours_ago), "favorite_count": likes,
              "retweet_count": 1, "reply_count": 0, "quote_count": 0, "conversation_id_str": reply_to or pid,
              "entities": {"urls": urls or []}}
    if reply_to:
        legacy["in_reply_to_status_id_str"] = reply_to
        legacy["in_reply_to_screen_name"] = reply_handle
    if media:
        legacy["extended_entities"] = {"media": media}
    r = {"__typename": "Tweet", "rest_id": pid, "legacy": legacy,
         "core": {"user_results": {"result": {"core": {"screen_name": handle, "name": handle.title()},
                                              "legacy": {"followers_count": 100}}}}}
    if quoted:
        legacy["quoted_status_id_str"] = quoted["rest_id"]
        r["quoted_status_result"] = {"result": quoted}
    if card:
        r["card"] = {"legacy": {"name": "summary_large_image", "binding_values": [
            {"key": k, "value": {"string_value": v}} for k, v in card.items()]}}
    if note:
        r["note_tweet"] = {"note_tweet_results": {"result": {"text": note}}}
    return r


def timeline_card(raw, hours_ago=1, retweeter=None):
    if retweeter:
        wrapper = result("rt" + raw["rest_id"], retweeter, "RT @x: ...", hours_ago)
        wrapper["legacy"]["retweeted_status_result"] = {"result": raw}
        return {"id": "rt" + raw["rest_id"], "createdAt": tw_time(hours_ago), "author": {"username": retweeter},
                "_raw": wrapper}
    return {"id": raw["rest_id"], "createdAt": tw_time(hours_ago), "author": {"username": raw["core"]["user_results"]["result"]["core"]["screen_name"]},
            "_raw": raw}


def sample_timeline():
    inner = result("100", "deep", "the original claim", 5, media=[{"type": "photo", "url": "https://t.co/m1",
                                                                     "media_url_https": "https://pbs.twimg.com/media/A.jpg"}])
    quote = result("101", "quoter", "this is wrong https://t.co/q1", 2, quoted=inner,
                   urls=[{"url": "https://t.co/q1", "expanded_url": "https://x.com/deep/status/100"}])
    article = result("102", "news", "Big story https://t.co/a1", 3,
                     card={"title": "Headline", "description": "Desc", "domain": "example.com", "card_url": "https://t.co/a1"},
                     urls=[{"url": "https://t.co/a1", "expanded_url": "https://example.com/story"}])
    longform = result("103", "writer", "truncated…", 1, note="the full long-form text")
    reply = result("104", "replier", "@news no it isn't", 1, reply_to="102", reply_handle="news")
    ad = result("105", "brand", "buy our thing", 24 * 9)
    rt = result("106", "funny", "a joke", 4, likes=500)
    return [timeline_card(quote, 2), timeline_card(article, 3), timeline_card(longform, 1), timeline_card(reply, 1),
            timeline_card(ad, 24 * 9), timeline_card(rt, 1, retweeter="friend1"), timeline_card(rt, 0.5, retweeter="friend2")]


class NormaliseTest(unittest.TestCase):
    def setUp(self):
        self.posts = {p["id"]: p for p in herald_x.normalise_timeline(sample_timeline())}

    def test_ads_dropped_and_retweets_merged(self):
        self.assertNotIn("105", self.posts)
        self.assertEqual(self.posts["106"]["retweeted_by"], ["friend1", "friend2"])
        self.assertEqual(self.posts["106"]["author"]["handle"], "funny")

    def test_quote_chain_media_and_stub_removal(self):
        q = self.posts["101"]
        self.assertEqual(q["text"], "this is wrong")
        self.assertEqual(q["quoted"]["author"]["handle"], "deep")
        self.assertEqual(q["quoted"]["media"][0]["url"], "https://pbs.twimg.com/media/A.jpg")
        self.assertEqual(q["quoted"]["text"], "the original claim")

    def test_card_links_and_note(self):
        a = self.posts["102"]
        self.assertEqual(a["card"]["title"], "Headline")
        self.assertEqual(a["card"]["url"], "https://example.com/story")
        self.assertIn("https://example.com/story", a["text"])
        self.assertEqual(self.posts["103"]["text"], "the full long-form text")
        self.assertEqual(self.posts["104"]["reply_to_id"], "102")


class StoreAndFeedTest(unittest.TestCase):
    def setUp(self):
        self.conn = herald_store.connect(os.path.join(TMP, f"{self._testMethodName}.db"))

    def test_ingest_resolve_and_complete_chains(self):
        posts, new = herald_feed.ingest_timeline(self.conn, sample_timeline())
        self.assertEqual(new, len(posts))
        _, again = herald_feed.ingest_timeline(self.conn, sample_timeline())
        self.assertEqual(again, 0)
        self.assertEqual(herald_store.get_post(self.conn, "106")["retweeted_by"], ["friend1", "friend2"])
        # reply to a post that is not stored, quote of a quote that is not inlined
        orphan = herald_x.normalise_result(result("200", "a", "reply", 1, reply_to="199", reply_handle="b"))
        deep = herald_x.normalise_result(result("201", "c", "quoting", 1, quoted=result("202", "d", "mid", 2)))
        deep["quoted"]["quoted_id"] = "203"
        for p in (orphan, deep):
            herald_store.upsert_post(self.conn, p)
        fetched = []

        def read(pid):
            fetched.append(pid)
            return herald_x.normalise_result(result(pid, "ctx", f"context {pid}", 3))
        n = herald_feed.complete_chains(self.conn, [orphan, deep], budget=5, read=read)
        self.assertEqual(sorted(fetched), ["199", "203"])
        self.assertEqual(n, 2)
        r = herald_feed.resolve(self.conn, orphan)
        self.assertEqual(r["parent"]["text"], "context 199")
        r = herald_feed.resolve(self.conn, deep)
        self.assertEqual(r["quoted"]["quoted"]["text"], "context 203")
        # context-only rows stay out of the window
        ids = {p["id"] for p in herald_store.posts_in_window(self.conn, 0, 2 ** 31)}
        self.assertNotIn("199", ids)
        self.assertNotIn("203", ids)


def make_post(pid, handle, text, t, likes=10, quoted=None, reply_to=None):
    return {"id": pid, "url": f"https://x.com/{handle}/status/{pid}", "author": {"handle": handle, "name": handle, "followers": 1},
            "seen_at": t, "posted_at": t, "text": text, "media": [], "card": None, "links": [], "quoted_id": quoted["id"] if quoted else None,
            "quoted": quoted, "reply_to_id": reply_to, "reply_to_handle": None, "conversation_id": None, "retweeted_by": [],
            "metrics": {"likes": likes, "retweets": 0, "replies": 0, "quotes": 0, "views": None}}


class WindowTest(unittest.TestCase):
    def test_threads_reposts_and_shared_quotes(self):
        outside = make_post("900", "source", "the announcement", 50)
        posts = [make_post("1", "alice", "part one", 100), make_post("2", "alice", "part two", 110, reply_to="1"),
                 make_post("3", "bob", "dumb take", 120, quoted=outside), make_post("4", "carol", "great take", 130, quoted=outside),
                 make_post("5", "dave", "same text", 140, likes=1), make_post("6", "dave", "same text", 150, likes=9)]
        w = herald_edit.Window(posts, 0, 1000)
        heads = [p["id"] for p in w.post.values()]
        self.assertNotIn("2", heads)                     # continuation folded into alice's thread
        self.assertEqual([t["id"] for t in w.thread(w.sid["1"])], ["2"])
        self.assertIn("900", heads)                      # quoted by two accounts -> shown once as r1
        self.assertTrue(w.sid["900"].startswith("r"))
        self.assertNotIn("5", heads)                     # repost kept once (the more-liked copy)
        self.assertIn("quoting [r1]", w.card(w.sid["3"]))


class QuoteCheckTest(unittest.TestCase):
    def test_quotes(self):
        q, ok = herald_edit.quotes_in('He said "this is the exact line" [p1] and \'another quoted phrase\' [p2].')
        self.assertTrue(ok)
        self.assertEqual(q, ["this is the exact line", "another quoted phrase"])
        self.assertFalse(herald_edit.quotes_in('bad " [p1]. x "')[1])
        self.assertTrue(herald_edit.quote_ok("it's  the exact… line", ["It’s the exact, line\nwith more"]))
        self.assertFalse(herald_edit.quote_ok("can never have enough power", ["engineered to never have enough power"]))
        self.assertEqual(herald_edit.normalise_cites("a [p1, p2] b [r3; p4]"), "a [p1][p2] b [r3][p4]")


def fake_chat_factory(responses):
    calls = []

    def chat(model, messages, **kw):
        step = "plan" if "editor of this reader" in messages[0]["content"] else "check" if "check an editor" in messages[0]["content"] else "write"
        calls.append(step)
        content = responses[step]() if callable(responses[step]) else responses[step]
        return json.dumps(content), {"model": model, "prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 0,
                                     "cost_usd": 0.001, "finish_reason": "stop"}
    return chat, calls


class BuildTest(unittest.TestCase):
    def window(self):
        posts = [make_post(str(i), f"user{i}", f"post number {i} about the treasury buyback", 100 + i, likes=i) for i in range(1, 9)]
        posts.append(make_post("9", "joker", "an unrelated joke about cats", 200, likes=99))
        return herald_edit.Window(posts, 0, 8 * 3600)

    def test_end_to_end_with_check_and_quote_repair(self):
        plan = {"mood": "Treasury day [p1][p2].", "entries": [
            {"type": "story", "priority": 1, "subject": "Treasury doubles buybacks", "posts": ["p1", "p2", "p3", "p9"], "feature": ["p1"]},
            {"type": "post", "priority": 1, "id": "p4", "setup": ""},
            {"type": "post", "priority": 3, "id": "p5", "setup": ""},
            {"type": "post", "priority": 2, "id": "p6", "setup": "context"},
            {"type": "post", "id": "p99"},
        ]}
        check = {"stories": [{"index": 0, "verdicts": [{"id": "p1", "belongs": True}, {"id": "p9", "belongs": False, "reason": "cats"}]}]}
        write = {"stories": [{"index": 0, "headline": "Buybacks", "body":
                              'The Treasury moved [p1]. One said "post number 2 about the treasury" [p2].\n\n'
                              'Another said "a sentence nobody wrote anywhere" [p3]. It was big [p3].'}]}
        chat, calls = fake_chat_factory({"plan": plan, "check": check, "write": write})
        with mock.patch.object(herald_llm, "chat", chat):
            data = herald_edit.build(self.window(), {"plan": "m", "check": "m", "write": "m"}, log=lambda m: None)
        story = data["entries"][0]
        self.assertEqual(story["type"], "story")
        self.assertNotIn("p9", story["posts"])                       # checker removed the unrelated post
        self.assertIn("post number 2 about the treasury", story["body"])
        self.assertNotIn("nobody wrote", story["body"])              # fabricated quote cut after retry
        self.assertIn("It was big [p3].", story["body"])
        self.assertEqual(calls.count("write"), 2)                    # draft + one revision
        self.assertTrue(any("p99" in i for i in data["issues"]))
        self.assertEqual({e.get("id") for e in data["entries"][1:]}, {"p4", "p5", "p6"})
        self.assertAlmostEqual(data["cost_usd"], 0.004)

    def test_trims_to_reading_budget(self):
        long = " ".join(["word"] * 85)
        posts = [make_post(str(i), f"u{i}", long, 100 + i) for i in range(1, 80)]
        w = herald_edit.Window(posts, 0, 8 * 3600)
        plan = {"mood": "", "entries": [{"type": "post", "priority": 3 if i > 20 else 2, "id": f"p{i}"} for i in range(1, 80)]}
        chat, _ = fake_chat_factory({"plan": plan, "check": {}, "write": {}})
        with mock.patch.object(herald_llm, "chat", chat), mock.patch.dict(os.environ, {"HERALD_READ_MINUTES": "10"}):
            data = herald_edit.build(w, {"plan": "m", "check": "m", "write": "m"}, log=lambda m: None)
        self.assertLessEqual(herald_render_text.reading_minutes(data), 10)
        prios = [e["priority"] for e in data["entries"]]
        self.assertEqual(prios.count(2), 20)                         # priority-2 posts all kept
        self.assertLess(prios.count(3), 59)                          # priority-3 posts dropped first


class SlotTest(unittest.TestCase):
    def test_windows_follow_sydney_time(self):
        s, e = herald.slot_window("morning", datetime(2026, 10, 18, 6, 5, tzinfo=herald.TZ))
        self.assertEqual(datetime.fromtimestamp(e, herald.TZ).hour, 6)
        self.assertEqual((e - s) / 3600, 16)
        s, e = herald.slot_window("afternoon", datetime(2026, 10, 18, 14, 2, tzinfo=herald.TZ))
        self.assertEqual(datetime.fromtimestamp(s, herald.TZ).hour, 6)
        self.assertEqual((e - s) / 3600, 8)
        s, e = herald.slot_window("morning", datetime(2026, 10, 18, 9, 0, tzinfo=herald.TZ))
        self.assertEqual(datetime.fromtimestamp(e, herald.TZ).day, 18)


class WebTest(unittest.TestCase):
    def test_pages_render(self):
        import herald_web
        conn = herald_store.connect()
        client = herald_web.app.test_client()
        self.assertIn(b"No digest yet", client.get("/").data)
        w = BuildTest().window()
        chat, _ = fake_chat_factory({"plan": {"mood": "Mood [p1] [p8].", "entries": [
            {"type": "post", "priority": 2, "id": "p6"}, {"type": "post", "priority": 2, "id": "p7"},
            {"type": "story", "priority": 1, "subject": "s", "posts": ["p1", "p2"], "feature": ["p1"]},
            {"type": "run", "priority": 2, "ids": ["p3", "p4"], "setup": "two posts"},
            {"type": "post", "priority": 1, "id": "p5", "setup": "<b>escaped</b>"}]},
            "check": {"stories": []}, "write": {"stories": [{"index": 0, "headline": "H", "body": "Body [p1][p2]."}]}})
        with mock.patch.object(herald_llm, "chat", chat):
            data = herald_edit.build(w, {"plan": "m", "check": "m", "write": "m"}, log=lambda m: None)
        did = herald_store.save_digest(conn, "afternoon", 0, 8 * 3600, "m", 0.01, data)
        conn.commit()
        page = client.get(f"/d/{did}").data.decode()
        self.assertIn('href="#p1"', page)                                        # on-page source
        self.assertIn('href="https://x.com/user8/status/8"', page)               # off-page source opens on X
        self.assertIn("&lt;b&gt;escaped&lt;/b&gt;", page)
        self.assertIn("You're caught up", page)
        for path in ("/", "/archive", "/debug"):
            self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(client.get("/d/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
