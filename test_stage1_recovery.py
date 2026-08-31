"""Stage-1 must recover from empty/malformed selection JSON."""
import herald_synthesise

assert herald_synthesise.parse_json_response('```json\n{"ok": true}\n```') == {"ok": True}
recovered = herald_synthesise.parse_json_response(
    '{"headline":"Test", "story":"A model said "unescaped words" [post:1]", "topic":"test"}'
)
assert recovered["headline"] == "Test" and '"unescaped words"' in recovered["story"]


posts = [{"id": "1", "text": "one", "author": {"username": "a"}, "metrics": {"likes": 1}}]
responses = iter([
    ('{"zeitgeist:":[],"stories_to_write":[],"items":[],"editor_notes":""}',
     {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.1}, "editor", "stop"),
    ('{"zeitgeist":[],"stories_to_write":[],"items":[{"post_id":"1","kind":"timeline","why":"visible"}],"editor_notes":""}',
     {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.2}, "editor", "stop"),
])
original = herald_synthesise._call
herald_synthesise._call = lambda *_args, **_kwargs: next(responses)
try:
    result, usage, _model, _finish = herald_synthesise.stage1_timeline_map(posts)
finally:
    herald_synthesise._call = original

assert result["items"][0]["post_id"] == "1"
assert usage["prompt_tokens"] == 4
assert usage["completion_tokens"] == 6
assert abs(usage["cost"] - 0.3) < 1e-9

# Frontier writers sometimes return prose with literal newlines inside a JSON
# string. That must parse as a story, not be rendered as a JSON blob.
raw_story = '''{"headline":"Readable", "story":"First paragraph.

Second paragraph. [post:1]", "topic":"test", "key_quotes":[]}'''
original = herald_synthesise._call
herald_synthesise._call = lambda *_args, **_kwargs: (raw_story, {}, "writer", "stop")
try:
    story, _usage, _model, _finish = herald_synthesise.stage2_story("test", posts)
finally:
    herald_synthesise._call = original
assert story["headline"] == "Readable"
assert "Second paragraph" in story["story"]
print("stage1 recovery tests pass")
