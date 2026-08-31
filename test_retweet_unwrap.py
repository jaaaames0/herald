"""Regression checks for inline Bird retweet unwrapping and quote hygiene."""

import collect
import parser
import herald_synthesise


wrapper = {
    "id": "wrapper-1",
    "text": "RT @source: wrapper text",
    "createdAt": "Wed Jul 22 22:53:02 +0000 2026",
    "author": {"id": "amplifier-id", "username": "amplifier", "name": "Amplifier"},
    "_raw": {"legacy": {"retweeted_status_result": {"result": {
        "rest_id": "original-1",
        "core": {"user_results": {"result": {
            "rest_id": "source-id",
            "core": {"screen_name": "source", "name": "Source"},
        }}},
        "legacy": {
            "id_str": "original-1",
            "full_text": "The original long-form source says bip110 is antagonistic to its own goals.",
            "created_at": "Wed Jul 22 20:48:14 +0000 2026",
            "conversation_id_str": "original-1",
            "favorite_count": 23,
            "retweet_count": 2,
            "reply_count": 2,
            "quote_count": 1,
        },
        "quoted_status_result": {"result": {
            "rest_id": "quoted-1",
            "core": {"user_results": {"result": {
                "rest_id": "quoted-author-id",
                "core": {"screen_name": "quoted", "name": "Quoted"},
            }}},
            "legacy": {
                "id_str": "quoted-1",
                "full_text": "The proposition being discussed.",
                "created_at": "Wed Jul 22 19:00:00 +0000 2026",
                "conversation_id_str": "quoted-1",
            },
        }},
    }}}},
}
assert collect.is_retweet(wrapper)
unwrapped = collect.unwrap_retweet(wrapper)
parsed = parser.parse_tweet(unwrapped)

assert parsed["id"] == "original-1"
assert parsed["author"]["username"] == "source"
assert parsed["amplified_by"]["username"] == "amplifier"
assert parsed["post_type"] == "quote"
assert "bip110 is antagonistic" in parsed["text"]
assert not parsed["text"].startswith("RT @")

posts = [{"id": "1", "text": "The actual source quote is here.", "author": {"username": "source"}}]
valid, rejected = herald_synthesise._validate_key_quotes([
    {"post_id": "1", "quote": "The actual source quote", "speaker": "source"},
    {"post_id": "1", "quote": "Starving … … … …", "speaker": "source"},
], posts)
assert len(valid) == 1
assert len(rejected) == 1

print("retweet unwrap and quote hygiene tests pass")
