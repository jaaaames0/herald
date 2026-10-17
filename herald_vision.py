"""Read the images attached to posts, the way a reader opening them would.

One vision call per image URL, cached forever (pbs.twimg.com URLs are
content-addressed). Video and GIF posts are read from their thumbnail.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import herald_llm
import herald_store

MODEL = os.environ.get("HERALD_VISION_MODEL", "qwen/qwen3.7-flash")
MAX_PER_RUN = int(os.environ.get("HERALD_VISION_MAX_PER_RUN", "400"))

PROMPT = """You are looking at an image attached to a post on X, as a reader who clicked on it.

Post by @{handle}: {text}

Return a JSON object:
{{"kind": one of "meme", "screenshot", "chart", "photo", "document", "video-still", "other",
 "text": the words visible in the image, transcribed verbatim (empty string if none; stop after about 500 characters),
 "description": one or two plain sentences on what the image shows. Never identify a person from their face or appearance; name someone only if the name is written in the image or in the post,
 "point": one sentence on what the image adds to the post - the joke, the claim, the evidence - or "" if it is decorative}}
Return only the JSON."""


def image_url(media):
    url = media["url"]
    if "pbs.twimg.com/media/" in url and "?" not in url:
        return url + "?name=medium"
    return url


def images_in(post):
    """(media, post) pairs for a post, its quote chain and its parent."""
    out = []
    seen = set()
    stack = [post]
    while stack:
        p = stack.pop()
        if not p or p["id"] in seen:
            continue
        seen.add(p["id"])
        for m in p.get("media") or []:
            out.append((m, p))
        stack.append(p.get("quoted"))
        stack.append(p.get("parent"))
    return out


def read_image(media, post, model=MODEL):
    kind = "video-still" if media["type"] != "photo" else "image"
    text = (post.get("text") or "")[:800] or "(no text)"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT.format(handle=post["author"]["handle"], text=text)
            + ("\n(This is the thumbnail of a video.)" if kind == "video-still" else "")},
        {"type": "image_url", "image_url": {"url": image_url(media)}},
    ]}]
    content, info = herald_llm.chat(model, messages, max_tokens=1200, temperature=0.1, json_mode=True, timeout=90)
    try:
        reading = herald_llm.parse_json(content)
    except (json.JSONDecodeError, ValueError):
        content, retry = herald_llm.chat(model, messages, max_tokens=1200, temperature=0.1, json_mode=True, timeout=90)
        info["cost_usd"] += retry["cost_usd"]
        reading = herald_llm.parse_json(content)
    if media.get("alt"):
        reading["alt"] = media["alt"]
    return reading, info


def read_window(conn, posts, model=MODEL, limit=MAX_PER_RUN, log=print, workers=8):
    """Read every unread image in the given (resolved) posts. Returns (count, cost)."""
    todo, seen = [], set()
    for post in posts:
        for media, owner in images_in(post):
            if media["url"] in seen or herald_store.get_reading(conn, media["url"]):
                continue
            seen.add(media["url"])
            todo.append((media, owner))
    todo = todo[:limit]
    cost = 0.0

    def work(item):
        media, owner = item
        try:
            return media, read_image(media, owner, model), None
        except (herald_llm.LLMError, json.JSONDecodeError, ValueError) as e:
            return media, None, e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for media, result, err in pool.map(work, todo):
            if result:
                reading, info = result
                herald_store.save_reading(conn, media["url"], "ok", reading, info["model"], info["cost_usd"])
                cost += info["cost_usd"]
            else:
                log(f"[vision] {media['url']}: {str(err)[:200]}")
                herald_store.save_reading(conn, media["url"], "failed", {"error": str(err)[:300]}, model, 0)
    conn.commit()
    return len(todo), cost
