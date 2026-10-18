"""Turn a window of the Following feed into a digest.

Three model calls:

1. plan   - the editor reads every post in the window (as cards carrying their
            quote chain, reply parent, image reading and link card) and decides
            what each selected thing is: a post that stands alone, a run of
            posts by one person, or a story many accounts are reacting to.
2. check  - every proposed story is checked: does each post really respond to
            the same specific thing? Posts that don't are removed. A story
            the reader already read in a recent edition is kept only if it
            has something new, and is then written as an update.
3. write  - stories are written from their posts' full cards. Every quoted
            phrase is checked verbatim against the source material and every
            citation against the story's posts; failures are retried once and
            then removed.

Posts themselves are never rewritten: the renderer shows them from storage.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import herald_llm
import herald_render_text
import herald_store

# --------------------------------------------------------------------- cards

def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%a %H:%M") if ts else "?"


def _image_lines(conn, post, indent):
    lines = []
    for m in post.get("media") or []:
        r = herald_store.get_reading(conn, m["url"]) if conn else None
        reading = (r or {}).get("reading") if r and r["status"] == "ok" else None
        label = "video" if m["type"] != "photo" else "image"
        if not reading:
            lines.append(f"{indent}[{label}: not read]" + (f" alt: {_clip(m.get('alt'), 200)}" if m.get("alt") else ""))
            continue
        parts = [f"{indent}[{label} · {reading.get('kind', '?')}]"]
        if reading.get("text"):
            parts.append(f'text in image: "{_clip(reading["text"], 400)}"')
        if reading.get("description"):
            parts.append(f"shows: {_clip(reading['description'], 250)}")
        if reading.get("point"):
            parts.append(f"point: {_clip(reading['point'], 200)}")
        lines.append(" — ".join(parts))
    return lines


def _link_line(post, indent):
    c = post.get("card")
    if c:
        return [f"{indent}[link] {_clip(c['title'], 160)} — {_clip(c.get('description'), 240)} ({c.get('domain')})"]
    if post.get("links"):
        return [f"{indent}[link] {post['links'][0]}"]
    return []


def _drop_reposts(posts):
    """Keep one copy when an author posts the same text twice (delete-and-repost, typo fixes)."""
    best = {}
    for p in posts:
        key = (p["author"]["handle"], re.sub(r"\W+", "", (p.get("text") or "").lower())[:200],
               tuple(m.get("type") for m in p.get("media") or []))
        if not key[1]:
            key = (p["author"]["handle"], p["id"], ())
        prior = best.get(key)
        if not prior or p["metrics"]["likes"] > prior["metrics"]["likes"]:
            best[key] = p
    return list(best.values())


class Window:
    """The posts in a window, grouped into self-threads and given short ids."""

    def __init__(self, posts, start, end, conn=None):
        self.conn, self.start, self.end = conn, start, end
        posts = _drop_reposts(posts)
        by_id = {p["id"]: p for p in posts}
        # Self-threads: a reply to the same author's post in the window continues it.
        head_of = {}
        for p in sorted(posts, key=lambda p: p["seen_at"] or 0):
            parent = by_id.get(p.get("reply_to_id") or "")
            if parent and parent["author"]["handle"] == p["author"]["handle"]:
                head_of[p["id"]] = head_of.get(parent["id"], parent["id"])
        self.threads = {}
        for p in sorted(posts, key=lambda p: p["seen_at"] or 0):
            h = head_of.get(p["id"])
            if h:
                self.threads.setdefault(h, []).append(p)
        heads = [p for p in posts if p["id"] not in head_of]
        heads.sort(key=lambda p: (p["seen_at"] or 0, p["id"]))
        # How many other accounts in the window quote or reply to each post.
        self.reactions = {}
        for p in posts:
            for target in {p.get("quoted_id"), p.get("reply_to_id")} - {None}:
                if target in by_id and by_id[target]["author"]["handle"] != p["author"]["handle"]:
                    self.reactions.setdefault(target, set()).add(p["author"]["handle"])
        self.sid = {}
        self.post = {}
        for i, p in enumerate(heads, 1):
            self.sid[p["id"]] = f"p{i}"
            self.post[f"p{i}"] = p
        # Posts outside the window that several window posts quote: show once.
        outside = {}
        for p in posts:
            q = p.get("quoted")
            if q and q["id"] not in by_id:
                outside.setdefault(q["id"], [q, set()])[1].add(p["author"]["handle"])
        n = 0
        for qid, (q, who) in outside.items():
            if len(who) >= 2:
                n += 1
                self.sid[qid] = f"r{n}"
                self.post[f"r{n}"] = q
                self.reactions[qid] = who
        self.authors = len({p["author"]["handle"] for p in posts})
        self.size = len(posts)

    def thread(self, sid):
        return self.threads.get(self.post[sid]["id"], [])

    def card(self, sid, full=True):
        p = self.post[sid]
        m = p["metrics"]
        head = f"[{sid}] {_time(p['seen_at'])} @{p['author']['handle']} ({p['author']['name']}) · {m['likes']}♥ {m['retweets']}↻"
        if p.get("retweeted_by"):
            head += " · retweeted by " + " ".join("@" + h for h in p["retweeted_by"][:6])
        if self.reactions.get(p["id"]):
            head += f" · quoted/replied to by {len(self.reactions[p['id']])} accounts here"
        if sid.startswith("r"):
            head += " · (not in the feed itself; quoted by posts in it)"
        lines = [head]
        parent = p.get("parent")
        if parent:
            ref = self.sid.get(parent["id"])
            lines.append(f"  ↪ replying to [{ref}]" if ref else
                         f"  ↪ replying to @{parent['author']['handle']}: \"{_clip(parent['text'], 300 if full else 160)}\"")
            if not ref:
                lines += _image_lines(self.conn, parent, "     ")
        elif p.get("reply_to_handle"):
            lines.append(f"  ↪ replying to @{p['reply_to_handle']} (post not available)")
        lines.append("  " + _clip(p["text"], 1100 if full else 500).replace("\n", "\n  "))
        for t in self.thread(sid):
            lines.append("  / " + _clip(t["text"], 700 if full else 300).replace("\n", "\n    "))
            lines += _image_lines(self.conn, t, "    ")
        lines += _image_lines(self.conn, p, "  ")
        lines += _link_line(p, "  ")
        q, depth = p.get("quoted"), 1
        while q and depth <= 4:
            ref = self.sid.get(q["id"])
            pad = "  " + "   " * depth
            if ref and ref != sid:
                lines.append(f"{pad}❝ quoting [{ref}]")
                break
            lines.append(f"{pad}❝ quoting @{q['author']['handle']}: \"{_clip(q['text'], 600 if full else 300)}\"")
            lines += _image_lines(self.conn, q, pad + "  ")
            lines += _link_line(q, pad + "  ")
            q, depth = q.get("quoted"), depth + 1
        if p.get("quoted_id") and not p.get("quoted"):
            lines.append("     ❝ quoting a post that is not available")
        return "\n".join(lines)

    def render(self, full=True):
        return "\n\n".join(self.card(s, full) for s in self.post)

    def material(self, sid):
        """All source text a story may quote from for this card."""
        p = self.post[sid]
        texts = []

        def add(x):
            if not x:
                return
            texts.append(x.get("text") or "")
            for m in x.get("media") or []:
                r = herald_store.get_reading(self.conn, m["url"]) if self.conn else None
                if r and r["status"] == "ok":
                    texts.append((r["reading"] or {}).get("text") or "")
                texts.append(m.get("alt") or "")
            if x.get("card"):
                texts.extend([x["card"].get("title") or "", x["card"].get("description") or ""])
        add(p)
        for t in self.thread(sid):
            add(t)
        q = p.get("quoted")
        while q:
            add(q)
            q = q.get("quoted")
        par = p.get("parent")
        while par:
            add(par)
            par = par.get("parent")
        return texts


# ------------------------------------------------------------------- prompts

READER = """The reader follows about 250 accounts on X and reads only the chronological Following feed: Bitcoin and markets people, AI and tech builders, US and Australian politics commentators, writers, shitposters. When they read X they click through: they follow quote-tweet chains to the bottom, open the post being replied to, open images (which often carry the text that matters) and open links. They care about what happened, what the people they follow are arguing about, and the jokes and moments that landed. They want the feed to shine through - its voices, its humour, its images - not a summary of it."""

PLAN_SYSTEM = READER + """

You are the editor of this reader's digest for one session of the feed. You will see every post from the window as a card: author, time, likes/retweets, which followed accounts retweeted it, how many accounts in the window quoted or replied to it, the post it replies to, the chain of posts it quotes, a reading of every image, and any link preview. Posts marked [r#] are not in the feed but were quoted by several posts in it. A line starting with "/" is the same author continuing their own thread.

Decide what the reader should see and what shape each thing takes. There are three shapes:

POST - a post that is the content. A joke, a striking image or screenshot, a sharp observation, a piece of news only one person posted, a good line. Most selected items are posts. Give a post a "setup" only when the reader would otherwise be lost (it reacts to something they can't see, it uses jargon, it's a reply whose parent matters); keep setups under 25 words, factual, and never explain a joke. An empty setup is often right.

RUN - two to four posts by the same person that belong together: an argument made across separate posts, a running commentary on one event, a bit they keep doing. Pick their best few, not all of them. Give one line on what connects them.

STORY - only when several different accounts (at least three, or two plus the original source they react to) are responding to the same specific thing: a named event, announcement, release, statement, number, viral post, or someone's action. Write its "subject" as one sentence naming that specific thing. Sharing a broad topic is NOT enough: "AI", "Bitcoin", "Australian politics", "the economy", "culture war" are topics, not stories. Apply this test to every post you put in a story: does this post directly respond to, report, or argue about the subject sentence? If you would have to say "this is also about..." or "in a similar vein...", it does not belong - make it a separate POST or leave it out. Never combine two different events into one story because they rhyme. If the thing everyone is reacting to is a specific post, article or announcement, include it (by its id) in the story's posts and list it first in "feature" so the reader sees the original.

What to select: the things that most of the feed was talking about this session (look at how many accounts touched a subject, retweets by follows, and reactions in the window, not only likes); the best individual posts, including humour, images and odd moments; and anything a well-informed friend would make sure they didn't miss. Skip routine self-promotion, giveaways, engagement bait, near-duplicates, and posts that make no sense without context you don't have. Don't pick the same post twice. Prefer the original over a post that merely restates it.

Size: about {stories} stories and at most {posts} posts/runs for this window - fewer if the window is quiet. The whole digest must fit in 10-15 minutes of reading, so choose; don't list everything good. Give every entry a "priority": 1 = the reader must see this (anything the whole feed reacted to, the very best jokes and images), 2 = strong, 3 = nice to have (dropped first if the digest runs long).

You may get PREVIOUSLY: the stories the reader read in the last editions. Don't run the same story with the same angle again. A continuing story earns a place only for what is new since then - a new development, number, reversal or turn, or a clearly new argument - and its subject should name that new thing (e.g. "Limoges city hall collapses as looting spreads to Bordeaux", not "riots in France"). If nothing is new, leave it out (or keep its single best new post as a POST) and use the room for something else.

Order the entries as a read: lead with the biggest story, then mix stories, runs and posts so similar things sit together and the reader moves through the session naturally. Put lighter items between heavier ones.

Return a JSON object:
{{"mood": "two or three sentences on what the feed was like this session - what dominated, what the mood was - citing at most four ids like [p12]",
 "entries": [
   {{"type": "story", "priority": 1, "subject": "one sentence naming the specific thing", "posts": ["p3", "p9", "r1", ...], "feature": ["r1"], "why": "why it matters to this reader, one sentence"}},
   {{"type": "run", "priority": 2, "ids": ["p20", "p25"], "setup": "one line"}},
   {{"type": "post", "priority": 1, "id": "p14", "setup": ""}}
 ]}}
Use only ids that appear in the cards. Return only the JSON."""

CHECK_SYSTEM = """You check an editor's groupings before publication. Each story has a subject sentence naming one specific thing, and a list of posts. For every post give a verdict: does it directly respond to, report, or argue about that specific subject? A post belongs if it is about the subject, even if it adds a joke, a chart or an opinion. A post does NOT belong if it only shares a broad topic, is about a related but different event, or would need "also"/"similarly" to connect it. Be strict about different events; don't remove posts just because they are short or jokey.

You may also get SETUPS: short lines the editor wrote to introduce a post. A setup is supported only if everything it states is shown in that post's card (its text, what it replies to or quotes, image text, link preview). A setup that adds a name, number, event or claim not in the card is unsupported, even if it is true.

You may also get PREVIOUSLY: stories the reader already read in recent editions, numbered. For each story, give "previous": the number of the earlier story it continues (the same ongoing event or argument), or null. If it continues one, give "fresh": true only if these posts establish something the earlier piece did not: a new development, event, number, reversal or turn, or a substantially new argument. More reactions, clips or jokes about what the earlier piece already covered are not fresh. With "fresh": true, give "new": one sentence naming what is new.

Return a JSON object: {"stories": [{"index": 0, "previous": null, "verdicts": [{"id": "p3", "belongs": true}, {"id": "p11", "belongs": false, "reason": "about a different event: ..."}]}, {"index": 1, "previous": 2, "fresh": true, "new": "...", "verdicts": [...]}], "setups": [{"id": "p14", "supported": true}, {"id": "p20", "supported": false, "reason": "..."}]}. Give a reason only for a false verdict. Return only the JSON."""

WRITE_SYSTEM = READER + """

You write the stories in this reader's digest. For each story you get the subject the editor chose and the full cards of the posts involved (with what they reply to, what they quote, image readings and link previews).

Write like a sharp, well-read friend who has been on the timeline all day: specific, dry, quick, a little wry when the material is funny. Lead with what actually happened, as established by the posts and their sources, then how the people in the feed reacted - who said what, where they disagree, the best lines. Let their voices carry it: quote short snippets verbatim and attribute them, e.g. @handle called it "the snippet" [p9]. The citation is shown as a link on the @handle you name, so name each account once per sentence and don't repeat it after the quote. If you don't name the account, the citation itself shows its handle.

Some stories are marked CONTINUING: the reader read an earlier piece on the same story (shown). Write these as continuing coverage: open with what has happened or changed since, and how the conversation has shifted. Assume the reader remembers the earlier piece - recap in one short clause at most, and don't retell or re-quote what it covered.

Rules, which are checked mechanically:
- Every paragraph cites at least one post by id, like [p9], and every factual claim is supported by a cited card. Use only the ids given for that story.
- Never repeat a quotation used in the earlier piece of a CONTINUING story.
- Use double quotation marks only for quoting a post; inside a quotation use single quotes.
- Text in quotation marks must be copied exactly from the cited post, its quoted posts, its parent, its image text or its link preview. Do not tidy, translate or combine quotes. If you are not sure of the exact words, paraphrase without quotation marks.
- Add nothing the cards don't establish: no outside facts, no numbers, dates, names or outcomes that aren't in them. If the posts disagree about a fact, say so.
- No moralising and no filler ("this highlights", "raises questions", "it remains to be seen", "in a world where"). Don't explain jokes. Don't use the word "timeline" more than once.
- Length follows the material: 80-180 words for most stories, up to 300 for the biggest. Short paragraphs.

Return a JSON object: {{"stories": [{{"index": 0, "headline": "specific and plain, under 12 words", "body": "paragraphs separated by blank lines"}}]}}. Return only the JSON."""


# ---------------------------------------------------------------- validation

_CITE = re.compile(r"\[([pr]\d+)\]")
_QCHARS = "\"“”"


_SINGLE = re.compile(r"(?<![A-Za-z0-9])['‘]([^'‘’\n]{12,}?)['’](?![A-Za-z0-9])")


def normalise_cites(text):
    """'[p1, p2]' or '[p1; p2]' -> '[p1][p2]'."""
    return re.sub(r"\[((?:[pr]\d+\s*[,;]\s*)+[pr]\d+)\]",
                  lambda m: "".join(f"[{x.strip()}]" for x in re.split(r"[,;]", m.group(1))), text or "")


def quotes_in(text):
    """Quoted spans in prose, pairing quotation marks in order.

    Returns (quotes, balanced). A span that contains a citation or starts with
    whitespace means the marks were mis-paired, so the text is unbalanced.
    Single-quoted spans of a dozen characters or more are treated as quotes
    too, so a writer can't dodge the verbatim check by switching marks.
    """
    text = (text or "").replace('\\"', '"')
    marks = [i for i, ch in enumerate(text) if ch in _QCHARS]
    if len(marks) % 2:
        return [], False
    out = []
    outside = []
    last = 0
    for a, b in zip(marks[::2], marks[1::2]):
        span = text[a + 1:b]
        if _CITE.search(span) or span[:1].isspace():
            return [], False
        if len(span) >= 12:
            out.append(span)
        outside.append(text[last:a])
        last = b + 1
    outside.append(text[last:])
    for chunk in outside:
        out += [m for m in _SINGLE.findall(chunk) if not _CITE.search(m) and not m[:1].isspace()]
    return out, True


def _norm(s):
    s = (s or "").lower().replace("&amp;", "&")
    s = s.translate(str.maketrans({"—": "-", "–": "-", "\u00a0": " "}))
    s = re.sub(r"[\"“”'‘’]", "", s)
    s = re.sub(r"[.,;:!?…]+(?=\s|$)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_ok(quote, sources):
    hay = [_norm(t) for t in sources if t]
    pieces = [x.strip(" .,;:!?-'") for x in re.split(r"…|\.\.\.|\[\.\.\.\]", _norm(quote))]
    pieces = [x for x in pieces if len(x) >= 4]
    return bool(pieces) and all(any(piece in h for h in hay) for piece in pieces)


def _requoted(quote, earlier):
    return bool(earlier) and _norm(quote)[:60] in _norm(earlier)


def check_body(body, allowed, window, earlier=""):
    """Return a list of problems with a story body.

    `earlier` is the text of a previous edition's piece this one continues.
    """
    problems = []
    material = [t for sid in allowed if sid in window.post for t in window.material(sid)]
    for para in [p for p in body.split("\n\n") if p.strip()]:
        cites = _CITE.findall(para)
        if not cites:
            problems.append(f"paragraph has no citation: {para[:80]!r}")
        for c in cites:
            if c not in allowed:
                problems.append(f"cites [{c}] which is not one of this story's posts")
    quotes, balanced = quotes_in(body)
    if not balanced:
        problems.append("quotation marks are unbalanced or nested; use single quotes inside a quotation")
    for q in quotes:
        if not quote_ok(q, material):
            problems.append(f"quoted text not found verbatim in the cited material: \"{q[:120]}\"")
        elif _requoted(q, earlier):
            problems.append(f"already quoted in the earlier piece the reader has read: \"{q[:120]}\"")
    return problems


def repair_body(body, allowed, window, earlier=""):
    """Drop sentences with unverifiable or repeated quotes or foreign citations; drop uncited paragraphs."""
    material = [t for sid in allowed if sid in window.post for t in window.material(sid)]
    out = []
    for para in [p for p in body.split("\n\n") if p.strip()]:
        sentences = re.split(r"(?<=[.!?”\"])\s+(?=[A-Z“\"@])", para.strip())
        keep = []
        for s in sentences:
            if any(c not in allowed for c in _CITE.findall(s)):
                continue
            quotes, balanced = quotes_in(s)
            if not balanced or any(not quote_ok(q, material) or _requoted(q, earlier) for q in quotes):
                continue
            keep.append(s)
        para = " ".join(keep)
        if para and _CITE.search(para):
            out.append(para)
    return "\n\n".join(out)


def attach_readings(conn, post):
    """Copy image readings into a post tree so a stored digest renders on its own."""
    import copy
    post = copy.deepcopy(post)
    stack = [post]
    while stack:
        p = stack.pop()
        if not p:
            continue
        for m in p.get("media") or []:
            r = herald_store.get_reading(conn, m["url"]) if conn else None
            if r and r["status"] == "ok":
                m["reading"] = r["reading"]
        stack += [p.get("quoted"), p.get("parent")] + list(p.get("thread") or [])
    return post


# -------------------------------------------------------------------- driver

def _call(model, system, user, max_tokens, log, label, usage, effort=None, temperature=0.5):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        t0 = time.time()
        content, info = herald_llm.chat(model, messages, max_tokens=max_tokens, temperature=temperature, json_mode=True,
                                        reasoning_effort=effort, timeout=600)
        info["seconds"] = round(time.time() - t0, 1)
        info["step"] = label
        usage.append(info)
        log(f"[{label}] {info['model']} in={info['prompt_tokens']} out={info['completion_tokens']} "
            f"cost=${info['cost_usd']:.4f} finish={info['finish_reason']} {info['seconds']}s")
        if info["finish_reason"] not in ("stop", None):
            log(f"[{label}] WARNING finish_reason={info['finish_reason']}")
        try:
            return herald_llm.parse_json(content)
        except (json.JSONDecodeError, ValueError) as e:
            log(f"[{label}] invalid JSON ({e}); retrying" if attempt == 0 else f"[{label}] invalid JSON again")
            messages = messages[:2] + [{"role": "assistant", "content": content[:2000]},
                                       {"role": "user", "content": "That reply was not valid, complete JSON. Reply again with only the JSON object, shorter if necessary."}]
    raise ValueError(f"{label}: model did not return valid JSON")


def _prio(e):
    try:
        return min(3, max(1, int(e.get("priority") or 2)))
    except (TypeError, ValueError):
        return 2


def _shown_posts(window, entries, mood):
    shown = set()
    for e in entries:
        shown.update(e.get("posts") or e.get("ids") or [e.get("id")])
    shown.update(c for c in _CITE.findall(mood) if c in window.post)
    shown.discard(None)
    posts = {}
    for sid in shown:
        p = dict(window.post[sid])
        p["thread"] = window.thread(sid)
        posts[sid] = attach_readings(window.conn, p)
    return posts


def match_written(written, stories, only=None):
    """Map written drafts to planned stories by the posts they cite (models renumber)."""
    out = {}
    candidates = [i for i in range(len(stories)) if only is None or i in only]
    for w in written:
        if not isinstance(w, dict):
            continue
        cites = set(_CITE.findall(normalise_cites(w.get("body") or "")))
        best = max(candidates, key=lambda i: (len(cites & set(stories[i]["posts"])), i == w.get("index")), default=None)
        if best is not None and best not in out and (cites & set(stories[best]["posts"]) or best == w.get("index")):
            out[best] = w
    return out


RUN_MAX = 4


def previously(digests):
    """Stories from earlier digests, with citations turned back into @handles."""
    out = []
    for d in digests:
        posts = d["data"].get("posts") or {}
        for e in d["data"].get("entries") or []:
            if e.get("type") != "story" or not e.get("body"):
                continue
            body = _CITE.sub(lambda m: f"(@{posts[m.group(1)]['author']['handle']})" if m.group(1) in posts else "",
                             e["body"])
            out.append({"digest": d["id"], "slot": d["slot"], "end": d["period_end"],
                        "headline": e.get("headline", ""), "subject": e.get("subject", ""), "body": body})
    return out


def size_for(hours):
    if hours <= 9:
        return 4, 22
    return 5, 26


def build(window, models, log=print, check=True, previous=()):
    """Return the digest data dict for a Window.

    `previous` is `previously(...)` for the last editions: repeats are dropped
    and continuing stories are written as updates.
    """
    usage, issues = [], []
    hours = (window.end - window.start) / 3600
    stories_n, posts_n = size_for(hours)
    cards = window.render()
    header = (f"Window: {_time(window.start)} to {_time(window.end)} UTC ({hours:.0f}h). "
              f"{window.size} posts from {window.authors} accounts.\n\n")
    previous = list(previous)
    if previous:
        cards += "\n\n=====\n\nPREVIOUSLY (stories the reader has already read):\n" + "\n".join(
            f"- {p['headline']}: {p['subject']}" for p in previous)
    plan = {}
    for attempt in range(2):
        plan = _call(models["plan"], PLAN_SYSTEM.format(stories=stories_n, posts=posts_n), header + cards,
                     12000, log, "plan", usage, models.get("plan_effort"), temperature=0.2)
        if len(plan.get("entries") or []) >= 5:
            break
        log(f"[plan] only {len(plan.get('entries') or [])} entries; retrying")

    used = set()
    entries = []
    for e in plan.get("entries") or []:
        t = e.get("type")
        if t == "story":
            ids = [i for i in dict.fromkeys(e.get("posts") or []) if i in window.post and i not in used]
            if len(ids) < 2:
                issues.append(f"story with <2 valid posts dropped: {e.get('subject')!r}")
                for i in ids:
                    entries.append({"type": "post", "id": i, "setup": ""})
                    used.add(i)
                continue
            feat = [i for i in e.get("feature") or [] if i in ids][:3]
            entries.append({"type": "story", "subject": e.get("subject", ""), "posts": ids, "feature": feat,
                            "why": e.get("why", ""), "priority": _prio(e)})
            used.update(ids)
        elif t == "run":
            ids = [i for i in dict.fromkeys(e.get("ids") or []) if i in window.post and i not in used]
            if len(ids) > RUN_MAX:
                keep = set(sorted(ids, key=lambda i: -window.post[i]["metrics"]["likes"])[:RUN_MAX])
                ids = [i for i in ids if i in keep]
            if ids:
                entries.append({"type": "run" if len(ids) > 1 else "post", "ids": ids, "id": ids[0],
                                "setup": e.get("setup", ""), "priority": _prio(e)})
                used.update(ids)
        elif t == "post" and e.get("id") in window.post and e.get("id") not in used:
            entries.append({"type": "post", "id": e["id"], "setup": e.get("setup", ""), "priority": _prio(e)})
            used.add(e["id"])
        else:
            if not (t == "post" and e.get("id") in used):
                issues.append(f"unusable entry dropped: {json.dumps(e)[:120]}")

    stories = [e for e in entries if e["type"] == "story"]
    setups = [e for e in entries if e["type"] != "story" and e.get("setup")]
    earlier = {}  # id(story) -> text of the earlier piece it continues
    repeats = 0
    if check and (stories or setups):
        text = []
        if previous and stories:
            text.append("PREVIOUSLY:\n\n" + "\n\n".join(
                f"PREVIOUS {n}: {p['headline']}\n{p['body']}" for n, p in enumerate(previous)))
        for i, s in enumerate(stories):
            text.append(f"STORY {i}: {s['subject']}\n" + "\n\n".join(window.card(sid, full=False) for sid in s["posts"]))
        if setups:
            text.append("SETUPS:\n\n" + "\n\n".join(
                f"SETUP for [{e['id']}]: {e['setup']}\n" + window.card(e["id"], full=False) for e in setups))
        verdict = _call(models["check"], CHECK_SYSTEM, "\n\n=====\n\n".join(text), 8000, log, "check", usage, temperature=0)
        bad = {x.get("id"): x.get("reason", "") for x in verdict.get("setups") or []
               if isinstance(x, dict) and x.get("supported") is False}
        for e in setups:
            if e["id"] in bad:
                issues.append(f"check dropped setup for {e['id']} ({e['setup'][:60]!r}): {bad[e['id']]}")
                e["setup"] = ""
        for v in verdict.get("stories") or []:
            idx = v.get("index")
            if not isinstance(idx, int) or not 0 <= idx < len(stories):
                continue
            s = stories[idx]
            removed = {x.get("id"): x.get("reason", "") for x in v.get("verdicts") or []
                       if isinstance(x, dict) and x.get("belongs") is False and x.get("id") in s["posts"]}
            for rid, why in removed.items():
                issues.append(f"check removed {rid} from {s['subject'][:60]!r}: {why}")
            s["posts"] = [p for p in s["posts"] if p not in removed]
            s["feature"] = [p for p in s["feature"] if p not in removed]
            s["removed"] = list(removed)
            prev = v.get("previous")
            if previous and isinstance(prev, int) and 0 <= prev < len(previous):
                p = previous[prev]
                if v.get("fresh") is True:
                    s["follows"] = {"digest": p["digest"], "slot": p["slot"], "headline": p["headline"],
                                    "new": (v.get("new") or "").strip()}
                    earlier[id(s)] = p["body"]
                else:
                    issues.append(f"repeat of {p['headline'][:50]!r} from an earlier edition, dropped: {s['subject'][:60]!r}")
                    s["repeat"] = True
                    repeats += 1
        for s in stories:
            if s.get("repeat"):
                continue
            accounts = {window.post[p]["author"]["handle"] for p in s["posts"]}
            if len(accounts) < 2:
                issues.append(f"story dissolved after check (one account left): {s['subject'][:60]!r}")
                s["dissolve"] = True
        new_entries = []
        for e in entries:
            if e.get("repeat"):
                continue
            if e.get("dissolve"):
                new_entries += [{"type": "post", "id": p, "setup": ""} for p in e["posts"][:1]]
            else:
                new_entries.append(e)
        entries = new_entries
        stories = [e for e in entries if e["type"] == "story"]

    # The planner tends to over-select; keep the best within the size asked for,
    # by priority, without disturbing reading order. A dropped repeat frees its
    # room for other posts.
    limit = posts_n + 6 + 3 * repeats
    singles = [e for e in entries if e["type"] != "story"]
    if len(singles) > limit:
        ranked = sorted(range(len(singles)), key=lambda i: (singles[i].get("priority", 2), i))
        keep = {id(singles[i]) for i in ranked[:limit]}
        for e in singles:
            if id(e) not in keep:
                issues.append(f"over plan size, dropped: {e.get('ids') or e.get('id')} (priority {e.get('priority')})")
        entries = [e for e in entries if e["type"] == "story" or id(e) in keep]

    if stories:
        text = []
        for i, s in enumerate(stories):
            cont = ""
            if s.get("follows"):
                cont = (f"CONTINUING: the reader read this earlier piece, \"{s['follows']['headline']}\":\n"
                        f"{earlier[id(s)]}\nWhat is new now: {s['follows']['new']}\n")
            text.append(f"STORY {i}\nSubject: {s['subject']}\nWhy it matters: {s['why']}\n{cont}Posts:\n\n"
                        + "\n\n".join(window.card(sid) for sid in s["posts"]))
        written = _call(models["write"], WRITE_SYSTEM, "\n\n=====\n\n".join(text), 12000, log, "write", usage,
                        models.get("write_effort"))
        by_idx = match_written(written.get("stories") or [], stories)
        retry = []
        for i, s in enumerate(stories):
            w = by_idx.get(i) or {}
            s["headline"], s["body"] = (w.get("headline") or s["subject"]).strip(), normalise_cites(w.get("body") or "").strip()
            s["problems"] = check_body(s["body"], s["posts"], window, earlier.get(id(s), "")) if s["body"] else ["empty body"]
            if s["problems"]:
                retry.append(i)
        if retry:
            text = []
            for i in retry:
                s = stories[i]
                cont = f"CONTINUING; the earlier piece the reader read:\n{earlier[id(s)]}\n" if s.get("follows") else ""
                text.append(f"STORY {i}\nSubject: {s['subject']}\n{cont}Your previous draft:\n{s['body']}\n\n"
                            f"Problems found:\n- " + "\n- ".join(s["problems"]) + "\n\nPosts:\n\n"
                            + "\n\n".join(window.card(sid) for sid in s["posts"]))
            fixed = _call(models["write"], WRITE_SYSTEM + "\n\nYou are revising drafts that failed the checks. Fix every listed problem.",
                          "\n\n=====\n\n".join(text), 6000, log, "rewrite", usage, models.get("write_effort"))
            for i, w in match_written(fixed.get("stories") or [], stories, only=retry).items():
                if i in retry and w.get("body"):
                    stories[i]["body"] = normalise_cites(w["body"]).strip()
                    stories[i]["headline"] = (w.get("headline") or stories[i]["headline"]).strip()
        for s in stories:
            problems = check_body(s["body"], s["posts"], window, earlier.get(id(s), ""))
            if problems:
                issues += [f"{s['headline'][:50]!r}: {p}" for p in problems]
                s["body"] = repair_body(s["body"], s["posts"], window, earlier.get(id(s), ""))
            s.pop("problems", None)
        dropped = [s for s in stories if not s["body"]]
        for s in dropped:
            issues.append(f"story dropped, nothing verifiable left: {s['headline']!r}")
        entries = [e for e in entries if not (e["type"] == "story" and not e["body"])]

    mood = normalise_cites(plan.get("mood") or "").strip()
    mood_cites = [c for c in _CITE.findall(mood) if c not in window.post]
    if mood_cites:
        mood = _CITE.sub(lambda m: m.group(0) if m.group(1) in window.post else "", mood)

    posts = _shown_posts(window, entries, mood)
    budget = float(os.environ.get("HERALD_READ_MINUTES", "14"))
    data = {"mood": mood, "entries": entries, "posts": posts}
    def over():
        return herald_render_text.reading_minutes(data) > budget

    def drop(level):
        cand = [i for i, e in enumerate(entries) if e["type"] != "story" and e.get("priority", 2) >= level]
        if not cand:
            return False
        gone = entries.pop(cand[-1])
        issues.append(f"trimmed for length: {gone.get('ids') or gone.get('id')} (priority {gone.get('priority')})")
        return True

    def shrink_runs():
        runs = [e for e in entries if e["type"] == "run" and len(e["ids"]) > 2]
        if not runs:
            return False
        r = max(runs, key=lambda e: len(e["ids"]))
        worst = min(r["ids"], key=lambda i: window.post[i]["metrics"]["likes"])
        r["ids"].remove(worst)
        issues.append(f"trimmed for length: {worst} from a run")
        return True

    for step in (lambda: drop(3), shrink_runs, lambda: drop(2)):
        while over() and step():
            data["posts"] = posts = _shown_posts(window, entries, mood)
    return {
        "window": {"start": window.start, "end": window.end, "hours": round(hours, 1),
                   "posts": window.size, "authors": window.authors},
        "mood": mood,
        "entries": entries,
        "posts": posts,
        "models": models,
        "usage": usage,
        "cost_usd": round(sum(u["cost_usd"] for u in usage), 5),
        "issues": issues,
    }
