#!/usr/bin/env python3
"""
parser.py — Parse and normalise tweets for Herald.

Input: unified JSON array on stdin (output of collect.py — RTs already
       resolved, age-gate applied). The parser is a pure data transformation
       with no bird dependencies.

Output: JSONL — one normalised tweet per line.

Source-agnostic schema: every entry has `post_type` ∈ {original, reply, quote}.
The collector resolves retweets upstream, so this parser never sees an RT.

Three fixes vs the prior newsroom iteration's bird_parse.py:
  1. parse_author: fall back to _raw.core.user_results.result.rest_id when
     top-level author.id is empty (which is almost always for non-RT tweets).
  2. parse_conversation: emit a STUB parent_tweet (id + url, text=null) because
     bird --json-full does NOT inline the parent tweet's content. Herald's
     pipeline fetches full parent text via `bird read <parent_id> --json-full`
     on demand if a story needs it.
  3. get_tweet_text: prefer note_tweet (long-form) text over outer text (which
     may be truncated for >280-char posts).

Plus: post_type uses the X-familiar vocabulary "retweet" (rather than the
generic "repost") because Herald is X-first and that's what readers expect.
Phase 3 source collectors can map other platforms' concepts (e.g., Mastodon
"boost") to "retweet" at the boundary.
"""

import json
import sys


def get_tweet_text(tweet):
    """Get the full text of a tweet, preferring note_tweet (long-form) over outer text.

    Note tweets are X posts >280 chars stored in _raw.note_tweet.note_tweet_results.result.text.
    The outer text is often truncated. Always check note_tweet first.
    """
    raw = tweet.get("_raw") or {}
    nt = raw.get("note_tweet") or {}
    ntr = nt.get("note_tweet_results") or {}
    result = ntr.get("result") or {}
    full = result.get("text")
    if full:
        return full
    return tweet.get("text", "")


def parse_author(tweet):
    """Extract lean author block.

    Top-level author.id is frequently empty in bird's --json-full output.
    Fall back to _raw.core.user_results.result.rest_id (the canonical user ID).
    """
    raw = tweet.get("_raw") or {}
    core = raw.get("core") or {}
    ur = core.get("user_results", {}).get("result") or {}
    user_legacy = ur.get("legacy") or {}

    author = tweet.get("author") or {}
    # rest_id is the canonical user ID; top-level author.id is often empty
    author_id = author.get("id") or ur.get("rest_id") or ""

    return {
        "username":        author.get("username", ""),
        "name":            author.get("name", ""),
        "id":              author_id,
        "followers_count": user_legacy.get("followers_count") or 0,
    }


def parse_metrics(tweet, legacy):
    """Extract lean metrics block."""
    return {
        "likes":       tweet.get("likeCount", 0) or 0,
        "retweets":    tweet.get("retweetCount", 0) or 0,
        "replies":     tweet.get("replyCount", 0) or 0,
        "quote_count": tweet.get("quoteCount", 0) or 0,
    }


def parse_conversation(tweet):
    """Extract lean conversation block. Emits a stub parent_tweet (id + url only)
    because bird --json-full does NOT inline the parent tweet's content.
    Herald's pipeline can fetch full parent text via `bird read <parent_id> --json-full`
    when a story actually needs it.
    """
    cid = tweet.get("conversationId", "")
    parent_id = tweet.get("inReplyToStatusId") or None
    is_reply = bool(parent_id)

    result = {
        "id": cid,
        "is_reply": is_reply,
        "parent_id": parent_id,
        "parent_tweet": None,
    }

    if is_reply and parent_id:
        # Stub — the pipeline decides whether to spend a fetch call.
        result["parent_tweet"] = {
            "id":         parent_id,
            "url":        f"https://x.com/_/status/{parent_id}",
            "text":       None,
            "author":     None,
            "needs_context_fetch": True,
        }

    return result


def parse_quoted(quoted):
    """Extract lean quoted_tweet block if present. Pulls the long-form note_tweet
    text from the quoted tweet's _raw if available (parity with parse_tweet).

    Includes the quoted tweet's `conversation` block (is_reply, parent_id,
    conversation_id) so herald_context's reply-parent walker knows whether
    to fetch the quoted tweet's own reply parent — this is what lets us
    unravel quote-of-reply-of-quote chains.

    When the underlying tweet has its own quotedTweet (a deeper level in the
    quote-tweet chain), recurse as a STUB (id + url, no text). Bird only inlines
    one level via --quote-depth 1; deeper levels are id-only in the JSON.
    herald_context.enrich_batch then chases the chain via bird read."""
    if not quoted:
        return None
    qauthor = quoted.get("author", {}) or {}
    quoted_text = get_tweet_text(quoted)
    # Mirror parse_conversation for the quoted tweet. Bird exposes both
    # `inReplyToStatusId` and `conversationId` on the inline JSON.
    q_parent_id = quoted.get("inReplyToStatusId") or None
    q_conv_id = quoted.get("conversationId", "") or ""
    block = {
        "id":         quoted.get("id", ""),
        "url":        f"https://x.com/{qauthor.get('username','')}/status/{quoted.get('id','')}",
        "text":       quoted_text,
        "author": {
            "username": qauthor.get("username", ""),
            "name":     qauthor.get("name", ""),
        },
        "created_at": quoted.get("createdAt", ""),
        "metrics": {
            "likes":       quoted.get("likeCount", 0) or 0,
            "retweets":    quoted.get("retweetCount", 0) or 0,
            "replies":     quoted.get("replyCount", 0) or 0,
            "quote_count": quoted.get("quoteCount", 0) or 0,
        },
        "conversation": {
            "id":           q_conv_id,
            "parent_id":    q_parent_id,
            "is_reply":     bool(q_parent_id),
            "parent_tweet": None,
        },
    }
    # Recurse one level for nested quotes — stub at this level (text=None)
    # so herald_context knows there's a deeper level to fetch.
    nested = quoted.get("quotedTweet")
    if nested:
        inner_id = nested.get("id", "")
        block["quoted_tweet"] = {
            "id":   inner_id,
            "url":  f"https://x.com/_/status/{inner_id}",
            "text": None,
            "author": None,
        }
    return block


def parse_media(tweet, legacy):
    """Extract media entries — type + media_url_https (for inline rendering).

    bird --json-full provides media_url_https for every media item. The prior
    version set url=None always; Herald needs the actual URL to hotlink images
    inline in the digest.
    """
    items = legacy.get("extended_entities", {}).get("media") or []
    if not items:
        return None
    parsed = []
    for m in items:
        mtype = m.get("type")
        if not mtype:
            continue
        item = {"type": mtype}
        url = m.get("media_url_https") or m.get("media_url")
        if url:
            item["url"] = url
        if mtype == "video":
            vi = m.get("video_info") or {}
            item["duration_ms"] = vi.get("duration_millis")
            variants = vi.get("variants") or []
            mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
            if mp4_variants:
                mp4_variants.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
                item["video_url"] = mp4_variants[0].get("url")
        parsed.append(item)
    return parsed if parsed else None


def parse_links(tweet, legacy):
    """Extract links array — expanded URLs (resolved from t.co) when available."""
    items = legacy.get("entities", {}).get("urls") or []
    if not items:
        return None
    links = []
    for lnk in items:
        url = lnk.get("expanded_url") or lnk.get("url")
        if url:
            links.append(url)
    return links if links else None


def parse_tweet(tweet):
    """Parse a single tweet dict from bird --json-full into a normalised entry.

    No RT handling here — the collector resolves RTs upstream.
    """
    raw = tweet.get("_raw") or {}
    legacy = raw.get("legacy") or {}
    author = parse_author(tweet)

    # Prefer note_tweet long-form text over outer text (which may be truncated)
    text = get_tweet_text(tweet)
    has_quoted = bool(tweet.get("quotedTweet"))
    is_reply = bool(tweet.get("inReplyToStatusId"))

    if has_quoted:
        post_type = "quote"
    elif is_reply:
        post_type = "reply"
    else:
        post_type = "original"

    return {
        "id":              tweet.get("id", ""),
        "url":             f"https://x.com/{tweet.get('author',{}).get('username','')}/status/{tweet.get('id','')}",
        "text":            text,
        "author":          author,
        "metrics":         parse_metrics(tweet, legacy),
        "conversation":    parse_conversation(tweet),
        "quoted_tweet":    parse_quoted(tweet.get("quotedTweet")),
        "media":           parse_media(tweet, legacy),
        "links":           parse_links(tweet, legacy),
        "created_at":      tweet.get("createdAt", ""),
        "lang":            legacy.get("lang") or tweet.get("lang"),
        "source":          legacy.get("source") or tweet.get("source"),
        "post_type":       post_type,
    }


def main():
    tweets = json.load(sys.stdin)
    if not isinstance(tweets, list):
        tweets = [tweets]

    for t in tweets:
        parsed = parse_tweet(t)
        print(json.dumps(parsed, ensure_ascii=False))


if __name__ == "__main__":
    main()
