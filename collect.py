#!/usr/bin/env python3
"""
collect.py — Herald's collector layer.

Reads raw `bird home --json-full` output (a JSON array), resolves retweets by
fetching originals via `bird read`, applies the age gate to drop ads, and
outputs a unified JSON array of "real" tweets ready for the parser.

The collector is the only layer that knows about bird; parser.py is
pure data transformation with no bird dependencies.

Why RTs go through `bird read`:
  Bird's `home --json-full` wrapper for RTs truncates the original text and
  buries the full text deep in `_raw.legacy.retweeted_status_result.result.note_tweet.note_tweet_results.result.text`
  — fragile, version-dependent. Re-fetching the original via `bird read <id> --json-full`
  returns it as a first-class tweet with normal structure. We don't care who
  retweeted (we just know someone we follow did, which is enough vetting
  signal). All context lives in the original.

Why the age gate:
  X surfaces promoted/ad tweets with old `createdAt` (days/weeks). The user's
  normal timeline pull is minutes-to-hours old. Anything > 24h in the feed
  pull is almost certainly a promoted ad — drop it. False-positive risk is
  near zero because the collector pulls only the most recent tweets.

Auth:
  bird requires Twitter auth_token + ct0 cookies. The collector sources them
  from `~/.config/herald/.env` if AUTH_TOKEN is not already in the environment.
  This makes the collector work uniformly from interactive shell, SSH,
  systemd, and cron contexts. The .env file should be chmod 600.

Usage:
    bird home -n 75 --following --json-full | collect.py > unified.json
    collect.py test_tweets.json > unified.json
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
    rt_resolved_count = 0
    rt_failed_count = 0
    age_dropped_count = 0
    age_unknown_kept = 0

    for t in tweets:
        tweet = t

        # 1. Resolve RTs by fetching the original via bird read
        if is_retweet(t):
            orig_id = get_original_tweet_id(t)
            if orig_id:
                original = resolve_retweet(orig_id)
                if original:
                    tweet = original
                    rt_resolved_count += 1
                else:
                    # Resolution failed — fall through to age gate on the wrapper
                    rt_failed_count += 1

        # 2. Apply age gate (on the possibly-resolved tweet)
        if not passes_age_gate(tweet):
            age_dropped_count += 1
            continue

        if not parse_twitter_date(tweet.get("createdAt") or tweet.get("created_at")):
            age_unknown_kept += 1

        resolved.append(tweet)

    print(
        f"[collect] input={len(tweets)} rt_resolved={rt_resolved_count} "
        f"rt_failed={rt_failed_count} age_dropped={age_dropped_count} "
        f"age_unknown_kept={age_unknown_kept} output={len(resolved)}",
        file=sys.stderr,
    )
    print(json.dumps(resolved, ensure_ascii=False))


if __name__ == "__main__":
    main()
