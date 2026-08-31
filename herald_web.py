"""herald_web.py — Herald's Flask web UI.

Routes:
  GET /                       list of recent digests (newest first)
  GET /digest/<id>            render one digest in full
  GET /debug                  pipeline_runs status (last 20 runs)

No auth — Herald serves public-domain-source material that's already on X,
and there's nothing to hide. Personal tool for James.

Runs under gunicorn on 127.0.0.1:8060; nginx terminates TLS and proxies.
"""
import datetime as _dt
import html
import json
import os
import secrets
import sys
from pathlib import Path

from flask import Flask, abort, render_template, request

# Ensure local module imports work under gunicorn
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import herald_db  # noqa: E402

app = Flask(__name__)
REVIEW_PASSWORD = os.environ.get("HERALD_REVIEW_PASSWORD", "")


@app.context_processor
def inject_globals():
    """Inject helpers into every Jinja2 template."""
    return {
        "now_year": _dt.datetime.now(tz=_dt.timezone.utc).year,
        "ts": _ts,
        "ago": _ago,
        "review_enabled": bool(REVIEW_PASSWORD),
    }


# ── helpers ──

def _ts(unix):
    if not unix:
        return "—"
    return _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ago(unix):
    if not unix:
        return ""
    delta = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 60: return f"{secs}s ago"
    if secs < 3600: return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _duration(start, end):
    if not start or not end:
        return ""
    secs = end - start
    if secs < 60: return f"{secs}s"
    if secs < 3600: return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def _salience_mark(s):
    return {"high": "★", "medium": "●", "low": "○"}.get(s or "", "·")


def _fmt_metrics(m):
    if not m: return ""
    if isinstance(m, str):
        try: m = json.loads(m)
        except: return ""
    parts = []
    for k in ("likes", "retweets", "replies"):
        v = m.get(k)
        if v is not None:
            parts.append(f"{v} {k}")
    return " · ".join(parts) if parts else ""


def _format_story(story_text):
    """Inline-format a story's prose for HTML. Preserves paragraph breaks.

    [post:ID] markers become hover-able citations linking to x.com.
    Inline "quoted text" — @author are highlighted.
    """
    if not story_text:
        return ""
    # Escape HTML first
    text = html.escape(story_text)
    # Replace [post:ID] with a styled citation chip linking to x.com
    import re as _re
    text = _re.sub(
        r"\[post:([A-Za-z0-9_:\.\-]+)\]",
        lambda m: f'<a class="cite" href="https://x.com/i/web/status/{m.group(1)}" target="_blank" rel="noopener" title="post {m.group(1)}">[{m.group(1)[:8]}\u2026]</a>',
        text,
    )
    # Bold inline quotes: "..." — @author
    text = _re.sub(
        r"\u201c([^\u201d]+?)\u201d\s*\u2014\s*@(\w+)",
        lambda m: f'<span class="quote">\u201c{m.group(1)}\u201d</span> <span class="quote-author\">\u2014 @{m.group(2)}</span>',
        text,
    )
    # Paragraphs on \n\n
    paragraphs = text.split("\n\n")
    return "\n".join(f"<p>{p.replace(chr(10), ' ')}</p>" for p in paragraphs if p.strip())


def _format_research(text):
    """Escape research prose while styling its validated [source:N] markers."""
    import re as _re
    escaped = html.escape(text or "")
    escaped = _re.sub(r"\[source:(\d+)\]", r'<span class="cite">[source:\1]</span>', escaped)
    return "".join(f"<p>{p}</p>" for p in escaped.split("\n\n") if p.strip())


def _require_review_access():
    """Keep unpublished desk material out of the public Herald site.

    The review interface is deliberately disabled unless an operator supplies
    a password via the service environment. HTTP Basic is browser-native and
    avoids putting a review secret in URLs or templates.
    """
    if not REVIEW_PASSWORD:
        abort(404)
    auth = request.authorization
    if not auth or auth.username != "herald" or not secrets.compare_digest(auth.password or "", REVIEW_PASSWORD):
        return False
    return True


def _review_challenge():
    return ("Review access required", 401, {"WWW-Authenticate": 'Basic realm="Herald review"'})


# ── routes ──

@app.route("/")
def index():
    conn = herald_db.get_connection()
    issues = herald_db.list_public_story_issues(conn, limit=20)
    for issue in issues:
        issue["published_ago"] = _ago(issue["published_at"])
        issue["published_str"] = _ts(issue["published_at"])
    conn.close()
    return render_template("index.html", issues=issues)


@app.route("/issue/<int:issue_id>")
def issue(issue_id):
    conn = herald_db.get_connection()
    try:
        data = herald_db.get_story_issue(conn, issue_id)
    finally:
        conn.close()
    if data.get("state") != "published":
        abort(404)
    for item in data.get("items") or []:
        item["story_html"] = _format_story(item.get("story") or "")
    data["published_str"] = _ts(data.get("published_at"))
    return render_template("issue.html", issue=data)


@app.route("/digest/<int:digest_id>")
def digest(digest_id):
    conn = herald_db.get_connection()
    d = herald_db.get_edition(conn, digest_id)
    conn.close()
    if not d:
        abort(404)
    # Pre-format stories for HTML rendering
    for s in d.get("stories") or []:
        s["story_html"] = _format_story(s.get("story") or "")
        # Format created_at on linked posts
        for lp in s.get("posts") or []:
            lp["created_str"] = _ts(lp.get("created_at"))
            lp["metrics_str"] = _fmt_metrics(lp.get("metrics"))
        # Decode key_quotes if JSON string
        kq = s.get("key_quotes")
        if isinstance(kq, str):
            try: s["key_quotes"] = json.loads(kq)
            except: s["key_quotes"] = []
    for card in d.get("edition_cards") or []:
        card["created_str"] = _ts(card.get("created_at"))
        card["metrics_str"] = _fmt_metrics(card.get("metrics"))
    if d.get("research_brief"):
        d["research_brief"]["body_html"] = _format_research(d["research_brief"].get("body"))
    d["generated_str"] = _ts(d["generated_at"])
    d["period_start_str"] = _ts(d["period_start"])
    d["period_end_str"] = _ts(d["period_end"])
    return render_template("digest.html", d=d, fmt_metrics=_fmt_metrics, salience_mark=_salience_mark)


@app.route("/desk")
def desk():
    if not _require_review_access():
        return _review_challenge()
    conn = herald_db.get_connection()
    try:
        issues = herald_db.list_story_issues(conn)
        queue = herald_db.list_story_review_queue(conn)
        for issue in issues:
            issue["created_str"] = _ts(issue["created_at"])
            issue["created_ago"] = _ago(issue["created_at"])
        for candidate in queue:
            candidate["last_seen_str"] = _ts(candidate["last_seen"])
            candidate["draft_ago"] = _ago(candidate.get("draft_created_at"))
    finally:
        conn.close()
    return render_template("desk.html", issues=issues, queue=queue)


@app.route("/desk/issue/<int:issue_id>")
def desk_issue(issue_id):
    if not _require_review_access():
        return _review_challenge()
    conn = herald_db.get_connection()
    try:
        issue = herald_db.get_story_issue(conn, issue_id)
    finally:
        conn.close()
    for item in issue.get("items") or []:
        item["story_html"] = _format_story(item.get("story") or "")
    issue["created_str"] = _ts(issue["created_at"])
    return render_template("desk_issue.html", issue=issue)


@app.route("/debug")
def debug():
    conn = herald_db.get_connection()
    runs = herald_db.list_pipeline_runs(conn, limit=30)
    # Add duration + ago columns
    for r in runs:
        r["started_str"] = _ts(r["started_at"])
        r["finished_str"] = _ts(r["finished_at"])
        r["duration_str"] = _duration(r["started_at"], r["finished_at"])
        r["ago_str"] = _ago(r["finished_at"] or r["started_at"])
        r["cost_str"] = f"${(r['cost_usd'] or 0):.6f}" if r.get("cost_usd") else ""
        r["tokens_str"] = f"{(r['prompt_tokens'] or 0):,}/{ (r['completion_tokens'] or 0):,}" if r.get("prompt_tokens") else ""
    # Last digest + last collect for status banner
    last_digest = conn.execute("SELECT id, generated_at, story_count FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    last_collect = conn.execute("SELECT * FROM pipeline_runs WHERE step='ingest' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return render_template("debug.html", runs=runs, last_digest=last_digest, last_collect=last_collect, ts=_ts, ago=_ago)


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    # Dev server only — production runs under gunicorn (see herald-web.service)
    app.run(host="127.0.0.1", port=8060, debug=False)
