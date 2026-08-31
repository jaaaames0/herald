"""digest_readable.py — Pretty-print a Herald digest as readable text.

Reads from the DB, no LLM call. Output is human-readable digest form
suitable for reading or sharing.

Usage: python3 digest_readable.py [digest_id]
"""
import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import herald_db


def ts(unix):
    if not unix:
        return ""
    return _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main():
    digest_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = herald_db.get_connection()
    if digest_id is None:
        row = conn.execute("SELECT id FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("no digests")
            return 1
        digest_id = row["id"]

    d = herald_db.get_digest_summary(conn, digest_id)

    # ── Cover ──
    print("=" * 70)
    print(f"  HERALD DIGEST #{digest_id}")
    print("=" * 70)
    hours = d.get("window_hours") or ((d["period_end"] - d["period_start"]) / 3600.0)
    print(f"  Window: {ts(d['period_start'])} → {ts(d['period_end'])}  ({hours:.0f}h)")
    print(f"  Generated: {ts(d['generated_at'])}")
    print(f"  Stories: {len(d['stories'])}")
    # Pipeline cost
    pr = conn.execute(
        "SELECT prompt_tokens, completion_tokens, cost_usd, model FROM pipeline_runs WHERE step='digest' AND rows_out=? ORDER BY id DESC LIMIT 1",
        (len(d["stories"]),),
    ).fetchone()
    if pr:
        pin = pr["prompt_tokens"] or 0
        pout = pr["completion_tokens"] or 0
        cost = pr["cost_usd"] or 0
        print(f"  Cost: ${cost:.6f} ({pin:,} in + {pout:,} out via {pr['model']})")
    print()

    # ── Editorial sections (zeitgeist, debates, signal vs noise) ──
    zeitgeist = d.get("zeitgeist") or []
    if zeitgeist:
        print("─" * 70)
        print("  ZEITGEIST — what's moving in the timeline")
        print("─" * 70)
        for th in zeitgeist:
            if not isinstance(th, dict):
                continue
            salience_marker = {"high": "★", "medium": "●", "low": "○"}.get(
                th.get("salience", ""), "?"
            )
            print()
            print(f"  {salience_marker} {th.get('label')}  ({th.get('salience')} salience)")
            print(f"    {th.get('description', '')}")
            accts = th.get("accounts") or []
            if accts:
                print(f"    Accounts: {', '.join('@' + a for a in accts)}")
            print(f"    Why it matters: {th.get('why_it_matters', '')}")
        print()

    # ── Stories ──
    print("=" * 70)
    print("  STORIES")
    print("=" * 70)
    for i, s in enumerate(d["stories"], 1):
        print()
        print(f"  ── {i}. {s.get('headline', '(no headline)')} ──")
        print(f"     [{s.get('topic', '?')}]  theme: {s.get('theme_label', '?')}")
        angle = s.get("angle")
        if angle:
            print(f"     angle: {angle}")
        print()
        story_text = s.get("story") or ""
        for line in story_text.split("\n"):
            print(f"     {line}")
        print()

        # Sources
        linked = s.get("posts") or []
        if linked:
            print(f"     Sources ({len(linked)}):")
            for lp in linked:
                ext_id = lp.get("external_id")
                username = lp.get("author_handle") or "?"
                ts_str = ts(lp.get("created_at"))
                txt = (lp.get("text") or "").replace("\n", " ")
                if len(txt) > 140:
                    txt = txt[:140] + "..."
                print(f"       · @{username} ({ts_str}) post:{ext_id}")
                print(f"         \"{txt}\"")
        print()
        # Key quotes (only show verified ones)
        kqs = s.get("key_quotes") or []
        if isinstance(kqs, str):
            try: kqs = json.loads(kqs)
            except: kqs = []
        if kqs:
            print(f"     Key quotes:")
            for kq in kqs:
                if not isinstance(kq, dict):
                    continue
                q = (kq.get("quote") or "")[:200]
                spk = (kq.get("speaker") or "?").lstrip("@")
                pid = kq.get("post_id", "?")
                print(f"       • \"{q}\" — @{spk} (post:{pid})")
        print()

    print("=" * 70)
    print(f"  end of digest #{digest_id}")
    print("=" * 70)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
