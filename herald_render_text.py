"""Render a digest as plain text, and estimate how long it takes to read."""

import re

WPM = 230
IMAGE_SECONDS = 4
# Long posts are folded after this many words ("show more"), as X does.
FOLD_WORDS = 90
# Self-threads show this many continuations; the rest are folded ("show thread").
THREAD_SHOWN = 1


def clip(text, words=FOLD_WORDS):
    parts = (text or "").split(" ")
    return text if len(parts) <= words else " ".join(parts[:words]) + " … [more]"


def _post_lines(p, indent=""):
    out = []
    par = p.get("parent")
    if par:
        out.append(f"{indent}  ↪ @{par['author']['handle']}: {par['text'][:280]}")
    out.append(f"{indent}@{p['author']['handle']}: {clip(p['text'])}")
    thread = p.get("thread") or []
    for t in thread[:THREAD_SHOWN]:
        out.append(f"{indent}  / {clip(t['text'])}")
    if len(thread) > THREAD_SHOWN:
        out.append(f"{indent}  / … {len(thread) - THREAD_SHOWN} more in thread")
    for m in p.get("media") or []:
        r = m.get("reading") or {}
        out.append(f"{indent}  [{m['type']}{' · ' + r['kind'] if r.get('kind') else ''}] "
                   + (f"\"{r['text'][:200]}\" " if r.get("text") else "") + (r.get("description") or ""))
    c = p.get("card")
    if c:
        out.append(f"{indent}  [link] {c['title']} ({c.get('domain')})")
    q = p.get("quoted")
    if q:
        out += _post_lines(q, indent + "    ❝ ")
    return out


def render(data):
    lines = [f"# Herald — {data['window']['hours']}h, {data['window']['posts']} posts from {data['window']['authors']} accounts",
             "", data.get("mood", ""), ""]
    posts = data["posts"]
    for e in data["entries"]:
        if e["type"] == "story":
            lines += ["", f"## {e['headline']}", e["body"], ""]
            for sid in e.get("feature") or []:
                lines += ["  > " + l for l in _post_lines(posts[sid])]
            lines.append("  sources: " + " ".join(f"[{s}] @{posts[s]['author']['handle']}" for s in e["posts"]))
        else:
            lines.append("")
            if e.get("setup"):
                lines.append(f"— {e['setup']}")
            for sid in e.get("ids") or [e["id"]]:
                lines += _post_lines(posts[sid])
    lines += ["", f"[reading time ~{reading_minutes(data):.0f} min · cost ${data.get('cost_usd', 0):.4f}]"]
    return "\n".join(lines)


def _words(s):
    return len(re.findall(r"\S+", s or ""))


def _post_cost(p):
    words, images = min(_words(p.get("text")), FOLD_WORDS), 0
    for t in (p.get("thread") or [])[:THREAD_SHOWN]:
        words += min(_words(t.get("text")), FOLD_WORDS)
    for m in p.get("media") or []:
        images += 1
        words += min(_words((m.get("reading") or {}).get("text")), 60) // 2
    if p.get("parent"):
        words += min(_words(p["parent"].get("text")), 60)
    if p.get("card"):
        words += _words(p["card"].get("title")) + 10
    if p.get("quoted"):
        w, i = _post_cost(p["quoted"])
        words, images = words + w, images + i
    return words, images


def reading_minutes(data):
    """Words of editor text plus every post shown in full; sources lists are folded away."""
    words, images = _words(data.get("mood")), 0
    for e in data["entries"]:
        shown = e.get("feature") or [] if e["type"] == "story" else e.get("ids") or [e["id"]]
        words += _words(e.get("body")) + _words(e.get("setup")) + _words(e.get("headline"))
        for sid in shown:
            w, i = _post_cost(data["posts"][sid])
            words, images = words + w, images + i
    return words / WPM + images * IMAGE_SECONDS / 60
