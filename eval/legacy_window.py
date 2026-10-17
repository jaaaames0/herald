"""Build an eval store from a window of the pre-October Herald database.

The old database kept post text, photo URLs, quoted-post text (quote_context)
and many reply parents, but not raw bird JSON, so link previews are missing
and quote chains stop at one level. Good enough to test selection,
grouping, images and writing on real historical days.

    python3 eval/legacy_window.py OLD_DB NEW_DB "2026-08-19 04:00" "2026-08-19 20:00"
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herald_store  # noqa: E402

TCO = re.compile(r"\s*https?://t\.co/[A-Za-z0-9]+")


def ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def convert(row, followers):
    m = json.loads(row["metrics"] or "{}")
    media = [x for x in json.loads(row["media_urls"] or "[]") if isinstance(x, dict) and x.get("url")]
    links = [u for u in json.loads(row["link_urls"] or "[]") if "t.co/" not in u and "x.com/" not in u]
    amp = json.loads(row["amplified_by"]) if row["amplified_by"] else None
    text = TCO.sub("", row["text"] or "").strip()
    return {
        "id": row["external_id"], "url": f"https://x.com/{row['author_handle']}/status/{row['external_id']}",
        "author": {"handle": row["author_handle"], "name": row["author_handle"], "followers": followers.get(row["author_handle"], 0)},
        "seen_at": row["created_at"], "posted_at": row["created_at"], "text": text,
        "media": [{"type": x.get("type", "photo"), "url": x["url"], "alt": ""} for x in media],
        "card": None, "links": links, "quoted_id": row["quoted_tweet_id"], "quoted": None,
        "reply_to_id": row["in_reply_to_id"], "reply_to_handle": None, "conversation_id": row["conversation_id"],
        "retweeted_by": [amp["username"]] if amp and amp.get("username") else [],
        "metrics": {"likes": m.get("likes", 0), "retweets": m.get("retweets", 0), "replies": m.get("replies", 0),
                    "quotes": m.get("quote_count", 0), "views": None},
    }


def convert_json(d):
    a = d.get("author") or {}
    m = d.get("metrics") or {}
    return {
        "id": str(d["id"]), "url": d.get("url") or "", "author": {"handle": a.get("username", ""), "name": a.get("name", ""), "followers": 0},
        "seen_at": None, "posted_at": None, "text": TCO.sub("", d.get("text") or "").strip(), "media": [], "card": None, "links": [],
        "quoted_id": ((d.get("quoted_tweet") or {}).get("id")), "quoted": None,
        "reply_to_id": (d.get("conversation") or {}).get("parent_id"), "reply_to_handle": None, "conversation_id": None,
        "retweeted_by": [], "metrics": {"likes": m.get("likes", 0), "retweets": m.get("retweets", 0), "replies": m.get("replies", 0), "quotes": m.get("quote_count", 0), "views": None},
    }


def main(old_path, new_path, start, end):
    old = sqlite3.connect(old_path)
    old.row_factory = sqlite3.Row
    new = herald_store.connect(new_path)
    followers = {r[0]: r[1] or 0 for r in old.execute("SELECT handle, follower_count FROM accounts")}
    rows = old.execute("SELECT * FROM posts WHERE created_at>=? AND created_at<? AND post_type!='retweet'", (ts(start), ts(end))).fetchall()
    wanted = set()
    for r in rows:
        herald_store.upsert_post(new, convert(r, followers))
        wanted.update(x for x in (r["quoted_tweet_id"], r["in_reply_to_id"]) if x)
    ctx = 0
    for pid in wanted:
        if herald_store.get_post(new, pid):
            continue
        r = old.execute("SELECT * FROM posts WHERE external_id=?", (pid,)).fetchone()
        if r:
            p = convert(r, followers)
        else:
            q = old.execute("SELECT data FROM quote_context WHERE quoted_tweet_id=? AND fetch_status='success'", (pid,)).fetchone()
            if not q:
                continue
            p = convert_json(json.loads(q[0]))
        herald_store.upsert_post(new, p, context_only=True)
        ctx += 1
    new.commit()
    print(f"{len(rows)} feed posts, {ctx} context posts -> {new_path}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
