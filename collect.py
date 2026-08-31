#!/usr/bin/env python3
"""
collect.py — Herald's collector layer.

Reads raw `bird home --json-full` output (a JSON array), applies the age gate
to drop ads, unwraps retweets from the inline card payload, and outputs the
observed timeline for the parser.

The collector is the only layer that knows about bird; parser.py is
pure data transformation with no bird dependencies.

Retweets are unwrapped locally from Bird's inline original payload. This avoids
an additional X request while retaining the person who amplified the post.

Why the age gate:
  X surfaces promoted/ad tweets with old `createdAt` (days/weeks). The user's
  normal timeline pull is minutes-to-hours old. Anything > 24h in the feed
  pull is almost certainly a promoted ad — drop it. False-positive risk is
  near zero because the collector pulls only the most recent tweets.

Auth:
  bird requires Twitter auth_token + ct0 cookies. Production supplies them
  through the protected worker EnvironmentFile. For manual development,
  explicitly point HERALD_ENV_PATH at a private file outside Git.

Usage:
    bird home -n 75 --following --json-full | collect.py > unified.json
    collect.py test_data/tweets_23072026.json > unified.json
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import herald_env
from herald_env import load_herald_env, HERALD_ENV_PATH  # re-export for back-compat


# Default age gate: drop tweets older than this (in hours).
# Ads are typically days/weeks old; normal feed pull is minutes/hours old.
DEFAULT_MAX_AGE_HOURS = 24

# Bird binary path (configurable via env for tests / alt installs)
BIRD_BIN = os.environ.get("HERALD_BIRD_BIN", "bird")


def parse_twitter_date(s):
    """Parse Twitter's `created_at` format: 'Sat Jun 27 06:08:59 +0000 2026'."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def is_retweet(tweet):
    """Detect if tweet is a retweet (by text prefix OR retweeted_status_result)."""
    if tweet.get("text", "").startswith("RT @"):
        return True
    raw = tweet.get("_raw") or {}
    legacy = raw.get("legacy") or {}
    if (legacy.get("retweeted_status_result") or {}).get("result"):
        return True
    return False


def get_original_tweet_id(tweet):
    """For an RT, return the original tweet's id (or None)."""
    raw = tweet.get("_raw") or {}
    legacy = raw.get("legacy") or {}
    rs = (legacy.get("retweeted_status_result") or {}).get("result") or {}
    return rs.get("rest_id")


def _result_to_tweet(result, observed_at, amplified_by):
    """Turn Bird's nested GraphQL Tweet result into the normal top-level shape.

    ``bird home`` includes the original of a retweet inline.  Reading it here
    avoids an extra X request and, unlike the wrapper text, retains Note Tweet
    long-form text, the original author, media, links, and quoted tweet data.
    ``observed_at`` intentionally remains the wrapper's timeline time: this
    represents when the user saw the post, not when its author first posted it.
    """
    legacy = result.get("legacy") or {}
    user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    user_core = user.get("core") or {}
    original_id = result.get("rest_id") or legacy.get("id_str")
    if not original_id:
        return None
    author = {
        "id": user.get("rest_id") or legacy.get("user_id_str") or "",
        "username": user_core.get("screen_name", ""),
        "name": user_core.get("name", ""),
    }
    quoted = ((result.get("quoted_status_result") or {}).get("result"))
    return {
        "id": str(original_id),
        "text": legacy.get("full_text") or "",
        "createdAt": observed_at or legacy.get("created_at") or "",
        "likeCount": legacy.get("favorite_count", 0) or 0,
        "retweetCount": legacy.get("retweet_count", 0) or 0,
        "replyCount": legacy.get("reply_count", 0) or 0,
        "quoteCount": legacy.get("quote_count", 0) or 0,
        "conversationId": legacy.get("conversation_id_str") or str(original_id),
        "inReplyToStatusId": legacy.get("in_reply_to_status_id_str") or None,
        "author": author,
        "quotedTweet": _result_to_tweet(quoted, observed_at, None) if quoted else None,
        "_raw": result,
        "amplified_by": amplified_by,
        "original_created_at": legacy.get("created_at") or None,
    }


def unwrap_retweet(tweet):
    """Return an inline original plus amplification provenance, if available."""
    raw = tweet.get("_raw") or {}
    legacy = raw.get("legacy") or {}
    result = ((legacy.get("retweeted_status_result") or {}).get("result"))
    if not result:
        return tweet
    wrapper_author = tweet.get("author") or {}
    amplified_by = {
        "id": wrapper_author.get("id", ""), "username": wrapper_author.get("username", ""),
        "name": wrapper_author.get("name", ""),
    }
    return _result_to_tweet(result, tweet.get("createdAt") or tweet.get("created_at"), amplified_by) or tweet


def resolve_retweet(tweet_id, timeout=30):
    """Fetch the original tweet via `bird read <id> --json-full`.
    Returns the parsed tweet dict, or None on failure."""
    try:
        result = subprocess.run(
            [BIRD_BIN, "read", str(tweet_id), "--json-full"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            print(f"WARN: bird read {tweet_id} failed: {result.stderr.strip()}", file=sys.stderr)
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"WARN: bird read {tweet_id} returned invalid JSON: {e}", file=sys.stderr)
            return None
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) else None
    except subprocess.TimeoutExpired:
        print(f"WARN: bird read {tweet_id} timed out", file=sys.stderr)
        return None
    except FileNotFoundError:
        print(f"ERROR: bird binary not found at {BIRD_BIN}", file=sys.stderr)
        sys.exit(2)


def passes_age_gate(tweet, max_age_hours=DEFAULT_MAX_AGE_HOURS):
    """Drop tweets older than max_age_hours. Returns True to keep."""
    created_str = tweet.get("createdAt") or tweet.get("created_at")
    created = parse_twitter_date(created_str)
    if not created:
        # Unknown age — keep (don't drop legitimate content on parse failure)
        return True
    age = datetime.now(timezone.utc) - created
    return age <= timedelta(hours=max_age_hours)


def main():
    # Load auth from .env if not already in the environment.
    # This must happen before any `bird` subprocess is spawned.
    load_herald_env()

    # Read input: stdin or filename arg
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            tweets = json.load(f)
    else:
        tweets = json.load(sys.stdin)

    if not isinstance(tweets, list):
        tweets = [tweets]

    resolved = []
    age_dropped_count = 0
    age_unknown_kept = 0

    for t in tweets:
        tweet = t

        # Apply the age gate without making additional X requests.
        if not passes_age_gate(tweet):
            age_dropped_count += 1
            continue

        if not parse_twitter_date(tweet.get("createdAt") or tweet.get("created_at")):
            age_unknown_kept += 1

        resolved.append(unwrap_retweet(tweet) if is_retweet(tweet) else tweet)

    # The requested count alone cannot tell us whether a cadence is covering
    # the feed: Bird normally returns exactly that many cards.  The observed
    # age span can.  A four-hour timer whose retained cards consistently reach
    # well past four hours has overlap; one that bottoms out below four hours
    # is clipping the timeline and needs a larger page or a shorter cadence.
    observed_ages = []
    now = datetime.now(timezone.utc)
    for tweet in resolved:
        created = parse_twitter_date(tweet.get("createdAt") or tweet.get("created_at"))
        if created:
            observed_ages.append(max(0.0, (now - created).total_seconds() / 60.0))
    coverage = "coverage_age_min=unknown"
    if observed_ages:
        coverage = (
            f"coverage_age_min={min(observed_ages):.1f}..{max(observed_ages):.1f}"
        )

    print(
        f"[collect] input={len(tweets)} retweets_unwrapped="
        f"{sum(1 for t in tweets if is_retweet(t))} age_dropped={age_dropped_count} "
        f"age_unknown_kept={age_unknown_kept} output={len(resolved)} {coverage}",
        file=sys.stderr,
    )
    print(json.dumps(resolved, ensure_ascii=False))


if __name__ == "__main__":
    main()
