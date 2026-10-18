"""Herald web UI: the digest is the page.

Routes: /  (latest digest)   /d/<id>   /archive   /debug
Read-only. Posts are rendered from the stored digest, never rewritten.
"""

import html
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, abort, render_template
from markupsafe import Markup

import herald_env

herald_env.load()

import herald_render_text  # noqa: E402
import herald_store  # noqa: E402

TZ = ZoneInfo(os.environ.get("HERALD_TZ", "Australia/Sydney"))
SLOT_HOURS = {"morning": 6, "afternoon": 14}
# Named for the Australian time they arrive; each covers a US session.
EDITIONS = {
    "morning": ("Morning edition", "the US day, overnight here"),
    "afternoon": ("Afternoon edition", "the US evening and the Australian day"),
}

app = Flask(__name__)

URL = re.compile(r"https?://[^\s<>\"']+")
MENTION = re.compile(r"(?<![\w/])@(\w{1,15})")
CITE = re.compile(r"\[([pr]\d+)\]")


def local(ts, fmt="%a %-I:%M%p"):
    return datetime.fromtimestamp(ts, TZ).strftime(fmt).replace("AM", "am").replace("PM", "pm") if ts else ""


def compact(n):
    n = n or 0
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 10_000 else str(n)


def post_html(text):
    """Escape post text, then link URLs and @mentions."""
    out = html.escape(text or "", quote=False)
    out = URL.sub(lambda m: f'<a href="{m.group(0)}" rel="noopener noreferrer" target="_blank">'
                  f'{re.sub(r"^https?://(www\.)?", "", m.group(0))[:48]}</a>', out)
    out = MENTION.sub(lambda m: f'<a href="https://x.com/{m.group(1)}" target="_blank" rel="noopener">@{m.group(1)}</a>', out)
    return Markup(out)


CITES = re.compile(r"(?:\s*\[[pr]\d+\])+")


def prose_html(text, posts, onpage=()):
    """Editor/writer prose: escape, paragraphs, and turn [p12] into a link to the source.

    If the sentence before a citation already names the post's author (@handle),
    that name becomes the link and the citation disappears (a second post by
    the same account shows as ↗); otherwise the citation shows as @handle. Sources shown on the page are linked in place;
    others open the post on X.
    """
    def link(sid, label, cls="cite"):
        if sid in onpage:
            return f'<a class="{cls}" href="#{sid}" data-sid="{sid}">{label}</a>'
        return f'<a class="{cls}" href="{html.escape(posts[sid]["url"])}" target="_blank" rel="noopener">{label}</a>'

    def para(p):
        out, last = [], 0
        for m in CITES.finditer(p):
            span, chips = html.escape(p[last:m.start()], quote=False), []
            for sid in CITE.findall(m.group(0)):
                if sid not in posts:
                    continue
                handle = html.escape(posts[sid]["author"]["handle"])
                named = re.compile(rf"(?<![\w>]){re.escape('@' + handle)}(?!\w)", re.I)
                if named.search(span):
                    span = named.sub(lambda n: link(sid, n.group(0), "cite named"), span, count=1)
                elif re.search(rf">@{re.escape(handle)}</a>", span, re.I):
                    chips.append(link(sid, "↗"))       # another post by an account already named here
                else:
                    chips.append(link(sid, f"@{handle}"))
            out.append(span + "".join(" " + c for c in chips))
            last = m.end()
        out.append(html.escape(p[last:], quote=False))
        return "".join(out)

    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return Markup("".join(f"<p>{para(p)}</p>" for p in paras))


def fold(text, words=herald_render_text.FOLD_WORDS):
    parts = (text or "").split(" ")
    if len(parts) <= words:
        return text, ""
    return " ".join(parts[:words]), " ".join(parts[words:])


def image_src(url):
    if "pbs.twimg.com/media/" in url and "?" not in url:
        return url + "?name=small"
    return url


app.jinja_env.filters.update(local=local, compact=compact, post_html=post_html, image_src=image_src)
app.jinja_env.globals.update(fold=fold, prose_html=prose_html, thread_shown=herald_render_text.THREAD_SHOWN)


def next_digest(now=None):
    now = now or datetime.now(TZ)
    for days in (0, 1):
        for h in sorted(SLOT_HOURS.values()):
            t = (now + timedelta(days=days)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now:
                return t.strftime("%a %-I%p").replace("AM", "am").replace("PM", "pm")
    return ""


def render_digest(d):
    data = d["data"]
    end = datetime.fromtimestamp(d["period_end"], TZ)
    title, covers = EDITIONS.get(d["slot"], (d["slot"].capitalize(), ""))
    onpage = set()
    for e in data["entries"]:
        onpage.update(e.get("posts") or e.get("ids") or [e.get("id")])
    return render_template(
        "digest.html", d=d, data=data, posts=data["posts"], onpage=onpage, title=title, covers=covers,
        date=end.strftime("%A %-d %B"), since=local(d["period_start"], "%a %-I%p"), until=local(d["period_end"], "%a %-I%p"),
        minutes=round(herald_render_text.reading_minutes(data)), next_digest=next_digest())


@app.route("/")
def index():
    conn = herald_store.connect()
    d = herald_store.get_digest(conn)
    if not d:
        return render_template("empty.html", next_digest=next_digest())
    return render_digest(d)


@app.route("/d/<int:digest_id>")
def digest(digest_id):
    conn = herald_store.connect()
    d = herald_store.get_digest(conn, digest_id)
    if not d:
        abort(404)
    return render_digest(d)


@app.route("/archive")
def archive():
    conn = herald_store.connect()
    rows = herald_store.list_digests(conn)
    for r in rows:
        r["when"] = local(r["period_end"], "%a %-d %b, %-I%p")
    return render_template("archive.html", rows=rows)


@app.route("/debug")
def debug():
    conn = herald_store.connect()
    runs = herald_store.recent_runs(conn)
    for r in runs:
        r["when"] = local(r["started_at"], "%a %-d %b %H:%M")
        r["secs"] = (r["finished_at"] - r["started_at"]) if r["finished_at"] else None
    caps = [dict(r) for r in conn.execute("SELECT * FROM captures ORDER BY id DESC LIMIT 12")]
    for c in caps:
        c["when"] = local(c["captured_at"], "%a %H:%M")
        c["span_h"] = round((c["captured_at"] - c["oldest_seen_at"]) / 3600, 1) if c["oldest_seen_at"] else None
    return render_template("debug.html", runs=runs, caps=caps)


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("HERALD_WEB_PORT", "8060")), debug=False)
