"""Load eval outputs into a preview database for the web UI.

    python3 eval/preview.py PREVIEW_DB WINDOW/TAG ...
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herald_store  # noqa: E402

db = herald_store.connect(sys.argv[1])
for spec in sys.argv[2:]:
    window, tag = spec.split("/")
    data = json.loads((Path("/tmp/herald-eval/out") / window / f"{tag}.json").read_text())
    slot = "morning" if data["window"]["hours"] > 12 else "afternoon"
    did = herald_store.save_digest(db, slot, data["window"]["start"], data["window"]["end"],
                                   f"{tag} ({data['models']['write']})", data["cost_usd"], data)
    print(did, spec)
db.commit()
