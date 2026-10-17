"""Score digest variants against a window's reference.

    python3 eval/score.py WINDOW [TAG ...]

Mechanical measures (the reference is a human judgement of the window):
  top5      reference conversations ranked 1-5 that the digest covers at all
  grouped   of those, how many are presented as one story with >=2 of their posts
  mixed     stories whose posts belong to two or more different reference
            conversations (the "unrelated tweets thrown together" failure)
  must      share of the must-see individual posts that were shown
  ads       promoted/self-promo posts that were selected (should be 0)
  quote     quotations that failed the verbatim check and had to be cut
  minutes   estimated reading time;  cost  USD for the digest incl. vision
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herald_render_text  # noqa: E402

OUT = Path("/tmp/herald-eval/out")


def score(ref, data):
    posts = data["posts"]
    rid = {sid: p["id"] for sid, p in posts.items()}
    conv_of = {}
    for c in ref["conversations"]:
        for pid in c["posts"]:
            conv_of.setdefault(pid, c["key"])
    shown = set(rid.values())
    for p in posts.values():
        shown.update(t["id"] for t in p.get("thread") or [])
    top = [c for c in ref["conversations"] if c["rank"] <= 5]
    covered = [c["key"] for c in top if shown & set(c["posts"])]
    grouped, mixed = [], []
    for e in data["entries"]:
        if e["type"] != "story":
            continue
        keys = Counter(conv_of.get(rid[s]) for s in e["posts"] if conv_of.get(rid[s]))
        if len(keys) > 1:
            mixed.append(f"{e['headline'][:60]!r}: {dict(keys)}")
        for c in top:
            if sum(1 for s in e["posts"] if rid[s] in c["posts"]) >= 2 and c["key"] not in grouped:
                grouped.append(c["key"])
    must = [p for p in ref["must_posts"] if p in shown]
    ads = [p for p in ref.get("skip_posts", []) if p in shown]
    quote_fail = sum(1 for i in data["issues"] if "quoted text not found" in i)
    return {
        "top5": f"{len(covered)}/{len(top)}", "missed": [c["key"] for c in top if c["key"] not in covered],
        "grouped": f"{len(grouped)}/{len(top)}", "mixed": mixed,
        "must": f"{len(must)}/{len(ref['must_posts'])}", "ads": len(ads), "quote_cut": quote_fail,
        "entries": len(data["entries"]), "stories": sum(e["type"] == "story" for e in data["entries"]),
        "minutes": round(herald_render_text.reading_minutes(data), 1), "cost": round(data["cost_usd"], 4),
        "models": f"{data['models'].get('plan')} / {data['models'].get('write')}",
    }


def main(window, tags):
    ref = json.loads((Path(__file__).parent / "reference" / f"{window}.json").read_text())
    tags = tags or sorted(p.stem for p in (OUT / window).glob("*.json"))
    for tag in tags:
        data = json.loads((OUT / window / f"{tag}.json").read_text())
        s = score(ref, data)
        print(f"{window}/{tag:<14} top5 {s['top5']} grouped {s['grouped']} must {s['must']} mixed {len(s['mixed'])} "
              f"ads {s['ads']} quote_cut {s['quote_cut']} | {s['entries']} entries, {s['stories']} stories, "
              f"{s['minutes']} min, ${s['cost']}  [{s['models']}]")
        for m in s["mixed"]:
            print(f"      mixed: {m}")
        if s["missed"]:
            print(f"      missed: {', '.join(s['missed'])}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
