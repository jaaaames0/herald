"""Normalise `bird --json-full` output into Herald posts.

Pure data: no network, no database. Everything a reader would click through
to on X is kept with the post — the quoted post (recursively), the reply
target, images with alt text, the link card, expanded links, long-form text,
and which followed accounts retweeted it.

Post shape:

    {
      "id", "url", "author": {"handle", "name", "followers"},
      "seen_at"    unix time the post appeared in the Following feed
                   (a retweet appears when it was retweeted),
      "posted_at"  unix time the author posted it,
      "text"       full text; media/quote t.co stubs removed, other t.co
                   links replaced by their expanded URL,
      "media": [{"type": "photo"|"video"|"animated_gif", "url", "alt"}],
      "card": {"title", "description", "domain", "url"} | None,
      "links": [expanded external URLs],
      "quoted_id", "quoted": <post> | None,
      "reply_to_id", "reply_to_handle", "conversation_id",
      "retweeted_by": [handles],
      "metrics": {"likes", "retweets", "replies", "quotes", "views"},
    }
"""

import re
from datetime import datetime

TCO = re.compile(r"https?://t\.co/[A-Za-z0-9]+")


def parse_time(s):
    if not s:
        return None
    try:
        return int(datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except ValueError:
        return None


def _tweet(result):
    """Unwrap GraphQL visibility wrappers to the Tweet object."""
    if not result:
        return None
    if result.get("__typename") == "TweetWithVisibilityResults" or (
            "tweet" in result and "legacy" not in result):
        result = result.get("tweet") or {}
    return result if result.get("legacy") else None


def _user(result):
    u = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = u.get("core") or {}
    legacy = u.get("legacy") or {}
    return {
        "handle": core.get("screen_name") or legacy.get("screen_name") or "",
        "name": core.get("name") or legacy.get("name") or "",
        "followers": legacy.get("followers_count") or 0,
    }


def _card(result):
    card = (result.get("card") or {}).get("legacy") or {}
    values = {}
    for item in card.get("binding_values") or []:
        v = item.get("value") or {}
        if "string_value" in v:
            values[item.get("key")] = v["string_value"]
    if not values.get("title"):
        return None
    url = values.get("card_url") or ""
    return {
        "title": values.get("title", "").strip(),
        "description": values.get("description", "").strip(),
        "domain": values.get("domain") or values.get("vanity_url") or "",
        "url": url,
    }


def _text_and_links(result, legacy, quoted_id, card):
    note = ((((result.get("note_tweet") or {}).get("note_tweet_results") or {})
             .get("result")) or {})
    text = note.get("text") or legacy.get("full_text") or ""
    urls = list((legacy.get("entities") or {}).get("urls") or [])
    urls += list((note.get("entity_set") or {}).get("urls") or [])
    expand = {u.get("url"): u.get("expanded_url") for u in urls if u.get("url")}
    media_stubs = {m.get("url") for m in (legacy.get("extended_entities") or {}).get("media") or []}
    media_stubs |= {m.get("url") for m in (legacy.get("entities") or {}).get("media") or []}

    links = []
    for short, full in expand.items():
        if full and not re.match(r"https?://(www\.)?(x|twitter)\.com/", full) and full not in links:
            links.append(full)

    def sub(m):
        short = m.group(0)
        if short in media_stubs:
            return ""
        full = expand.get(short)
        if full and quoted_id and quoted_id in full:
            return ""
        if card and short == card.get("url") and not full:
            return ""
        return full or short

    text = TCO.sub(sub, text)
    if card and card.get("url"):
        card["url"] = expand.get(card["url"]) or (links[0] if links else card["url"])
    return text.strip(), links


def _media(legacy):
    out = []
    for m in (legacy.get("extended_entities") or {}).get("media") or []:
        url = m.get("media_url_https") or m.get("media_url")
        if url:
            out.append({"type": m.get("type") or "photo", "url": url,
                        "alt": (m.get("ext_alt_text") or "").strip()})
    return out


def normalise_result(result, seen_at=None, depth=0):
    """Turn one GraphQL Tweet result into a Herald post (recursing into quotes)."""
    result = _tweet(result)
    if not result:
        return None
    legacy = result["legacy"]
    pid = result.get("rest_id") or legacy.get("id_str")
    author = _user(result)
    quoted_id = legacy.get("quoted_status_id_str")
    quoted_result = (result.get("quoted_status_result") or {}).get("result")
    quoted = normalise_result(quoted_result, None, depth + 1) if quoted_result and depth < 4 else None
    if quoted:
        quoted_id = quoted["id"]
    card = _card(result)
    text, links = _text_and_links(result, legacy, quoted_id, card)
    views = (result.get("views") or {}).get("count")
    posted_at = parse_time(legacy.get("created_at"))
    return {
        "id": str(pid),
        "url": f"https://x.com/{author['handle']}/status/{pid}",
        "author": author,
        "seen_at": seen_at or posted_at,
        "posted_at": posted_at,
        "text": text,
        "media": _media(legacy),
        "card": card,
        "links": links,
        "quoted_id": quoted_id,
        "quoted": quoted,
        "reply_to_id": legacy.get("in_reply_to_status_id_str"),
        "reply_to_handle": legacy.get("in_reply_to_screen_name"),
        "conversation_id": legacy.get("conversation_id_str"),
        "retweeted_by": [],
        "metrics": {
            "likes": legacy.get("favorite_count") or 0,
            "retweets": legacy.get("retweet_count") or 0,
            "replies": legacy.get("reply_count") or 0,
            "quotes": legacy.get("quote_count") or 0,
            "views": int(views) if views and str(views).isdigit() else None,
        },
    }


def normalise_timeline(cards, max_age_hours=48):
    """Normalise a `bird home --json-full` array.

    Retweets are unwrapped to the original post with the retweeter recorded;
    the same post retweeted by several follows becomes one post. Promoted
    posts are dropped: X serves them with an old posting time, so a
    non-retweet older than ``max_age_hours`` at capture is treated as an ad.
    """
    out = {}
    newest = max((parse_time(c.get("createdAt")) or 0 for c in cards), default=0)
    for card in cards:
        raw = card.get("_raw") or {}
        legacy = raw.get("legacy") or {}
        seen_at = parse_time(card.get("createdAt"))
        rt = (legacy.get("retweeted_status_result") or {}).get("result")
        if rt:
            post = normalise_result(rt, seen_at)
            retweeter = (card.get("author") or {}).get("username") or _user(raw)["handle"]
        else:
            post = normalise_result(raw, seen_at)
            retweeter = None
            if post and newest and post["posted_at"] and newest - post["posted_at"] > max_age_hours * 3600:
                continue
            if post and "unified_card" in str((raw.get("card") or {}).get("legacy", {}).get("name", "")):
                continue
        if not post:
            continue
        prior = out.get(post["id"])
        if prior:
            post["seen_at"] = min(prior["seen_at"] or post["seen_at"], post["seen_at"] or prior["seen_at"])
            post["retweeted_by"] = prior["retweeted_by"]
        if retweeter and retweeter not in post["retweeted_by"]:
            post["retweeted_by"].append(retweeter)
        out[post["id"]] = post
    return list(out.values())


def normalise_read(data):
    """Normalise `bird read <id> --json-full` output (one tweet or a list)."""
    if isinstance(data, list):
        data = data[0] if data else None
    if not data:
        return None
    return normalise_result(data.get("_raw") or {})
