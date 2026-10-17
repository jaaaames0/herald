"""Run one digest variant on one eval window.

    python3 eval/run.py WINDOW TAG --plan MODEL --write MODEL [--check MODEL] [--no-check]

Writes /tmp/herald-eval/out/WINDOW/TAG.{json,txt}. Uses NanoGPT credit.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herald  # noqa: E402
import herald_render_text  # noqa: E402
import herald_store  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("window")
ap.add_argument("tag")
ap.add_argument("--plan", required=True)
ap.add_argument("--write")
ap.add_argument("--check", default="anthropic/claude-haiku-5.5")
ap.add_argument("--no-check", action="store_true")
ap.add_argument("--effort")
args = ap.parse_args()

w = json.loads((Path(__file__).parent / "windows.json").read_text())[args.window]
conn = herald_store.connect(w["store"])
models = {"plan": args.plan, "write": args.write or args.plan, "check": args.check}
if args.effort:
    models["plan_effort"] = models["write_effort"] = args.effort
did, data = herald.make_digest(conn, herald.parse_when(w["start"]), herald.parse_when(w["end"]), "eval",
                               models=models, check=not args.no_check)
out = Path("/tmp/herald-eval/out") / args.window
out.mkdir(parents=True, exist_ok=True)
(out / f"{args.tag}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
(out / f"{args.tag}.txt").write_text(herald_render_text.render(data))
print(f"{args.window}/{args.tag}: entries={len(data['entries'])} stories={sum(e['type']=='story' for e in data['entries'])} "
      f"minutes={herald_render_text.reading_minutes(data):.1f} cost=${data['cost_usd']:.4f} issues={len(data['issues'])}")
