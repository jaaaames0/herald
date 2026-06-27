"""End-to-end validation: confirm parser output meets all expected properties."""
import json
import sys
from collections import Counter

parsed = []
with open(sys.argv[1]) as f:
    for line in f:
        line = line.strip()
        if line:
            parsed.append(json.loads(line))

print(f"Total parsed entries: {len(parsed)}\n")

# 1. Every entry has a non-empty author.id (the fix)
empty_id = [p for p in parsed if not p.get("author", {}).get("id")]
print(f"Entries with empty author.id: {len(empty_id)}")
if empty_id:
    for p in empty_id[:3]:
        print(f"  @{p.get('author', {}).get('username', '?')}")

# 2. post_type distribution
types = Counter(p.get("post_type") for p in parsed)
print(f"\npost_type distribution: {dict(types)}")
unexpected_types = [t for t in types if t not in ("original", "reply", "quote")]
if unexpected_types:
    print(f"  ⚠ Unexpected post_type values: {unexpected_types}")
else:
    print(f"  ✓ All post_type values are in (original, reply, quote)")

# 3. Media URLs populated
media_count = 0
media_with_url = 0
for p in parsed:
    media = p.get("media") or []
    if media:
        media_count += len(media)
        for m in media:
            if m.get("url"):
                media_with_url += 1
print(f"\nMedia items: {media_count}, with URL: {media_with_url} ({'✓' if media_count == media_with_url else '✗'})")

# 4. Starlink and Figma Weave should be GONE (age-gated)
starlink_present = any("starlink" in (p.get("text") or "").lower() for p in parsed)
figma_weave_present = any(
    "figma weave" in (p.get("text") or "").lower() for p in parsed
)
print(f"\nAd check:")
print(f"  Starlink in output: {starlink_present} ({'✗' if starlink_present else '✓ dropped'})")
print(f"  Figma Weave in output: {figma_weave_present} ({'✗' if figma_weave_present else '✓ dropped'})")

# 5. RT-resolution check: the 6 RT originals should now be first-class tweets
# Heuristic: their text should NOT start with "RT @", and they should be the
# resolved originals (full text, not the wrapper)
rt_resolved_ok = 0
rt_resolved_bad = []
for p in parsed:
    text = p.get("text", "")
    if not text.startswith("RT @"):
        # Could be a regular tweet OR a successfully-resolved RT
        # (since the resolved RT's text is the original's, not the wrapper's)
        # We can't directly tell, but we CAN check for the original tweet IDs
        # that we tried to resolve.
        pass

# Direct check: do the resolved original IDs appear in the parsed output?
import subprocess
collect_log = open("/home/james/srv/herald/collect.log").read()
print(f"\nCollector log: {collect_log.strip()}")

# Find the 6 RT original IDs that we tried to resolve (from collect.log)
# They're in the WARN messages — wait, we don't have WARN messages now.
# Let me look for them differently — compare input to output.
import json as _json
input_tweets = _json.load(open("/home/james/srv/herald/test_tweets.json"))
input_ids = {t["id"] for t in input_tweets}
output_ids = {p["id"] for p in parsed}

# The 6 RT wrappers in input should NOT be in output (they were replaced)
# The 6 resolved originals SHOULD be in output
print(f"\nInput tweet IDs: {len(input_ids)}")
print(f"Output tweet IDs: {len(output_ids)}")
replaced = input_ids - output_ids
added = output_ids - input_ids
print(f"Replaced (RT wrappers): {len(replaced)}")
print(f"Added (resolved originals): {len(added)}")
if len(replaced) == 6 and len(added) == 6:
    print(f"  ✓ 6 RTs replaced with 6 resolved originals")
else:
    print(f"  ✗ Expected 6/6, got {len(replaced)}/{len(added)}")

# 6. Note tweet check (should still have long-form text)
print(f"\nNote tweet check:")
for p in parsed:
    if len(p.get("text", "")) > 280:
        print(f"  ✓ id={p['id']} text_len={len(p['text'])} author=@{p.get('author', {}).get('username', '?')}")
        print(f"    preview: {p['text'][:100]!r}...")

# 7. Sample of a successfully-resolved RT original
# Find one that wasn't in the input (i.e., a resolved original)
if added:
    sample_id = list(added)[0]
    for p in parsed:
        if p["id"] == sample_id:
            print(f"\n=== Sample resolved RT original (id={sample_id}) ===")
            print(f"  text_len: {len(p['text'])}")
            print(f"  text preview: {p['text'][:200]!r}")
            print(f"  author: @{p['author']['username']} (id={p['author']['id']})")
            print(f"  metrics: {p['metrics']}")
            break
