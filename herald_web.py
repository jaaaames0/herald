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
import sys
from pathlib import Path

from flask import Flask, abort, render_template

# Ensure local module imports work under gunicorn
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import herald_db  # noqa: E402

app = Flask(__name__)


@app.context_processor
def inject_globals():
    """Inject helpers into every Jinja2 template."""
    return {
        "now_year": _dt.datetime.now(tz=_dt.timezone.utc).year,
        "ts": _ts,
        "ago": _ago,
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


# ── routes ──

@app.route("/")
def index():
    conn = herald_db.get_connection()
    digests = herald_db.list_digests(conn, limit=20)
    # annotate with cost + token usage from the corresponding pipeline_run
    for d in digests:
        d["generated_ago"] = _ago(d["generated_at"])
        pr = conn.execute(
            "SELECT cost_usd, prompt_tokens, completion_tokens, model FROM pipeline_runs "
            "WHERE step='digest' AND rows_out=? AND id <= (SELECT MAX(id) FROM pipeline_runs WHERE step='digest' AND rows_out=? AND finished_at IS NOT NULL) "
            "ORDER BY id DESC LIMIT 1",
            (d["story_count"], d["story_count"]),
        ).fetchone()
        if pr:
            d["cost_usd"] = pr["cost_usd"]
            d["tokens_in"] = pr["prompt_tokens"]
            d["tokens_out"] = pr["completion_tokens"]
            d["model"] = pr["model"]
        else:
            d["cost_usd"] = None
            d["tokens_in"] = d["tokens_out"] = 0
            d["model"] = ""
        d["input_count"] = herald_db.count_posts_in_digest(conn, d["id"])
    conn.close()
    return render_template("index.html", digests=digests, ts=_ts)


@app.route("/digest/<int:digest_id>")
def digest(digest_id):
    conn = herald_db.get_connection()
    d = herald_db.get_digest_summary(conn, digest_id)
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
    d["generated_str"] = _ts(d["generated_at"])
    d["period_start_str"] = _ts(d["period_start"])
    d["period_end_str"] = _ts(d["period_end"])
    return render_template("digest.html", d=d, fmt_metrics=_fmt_metrics, salience_mark=_salience_mark)


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
