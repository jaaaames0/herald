#!/usr/bin/env python3
"""Print the private evidence-and-model dossier for a Herald digest.

Usage: HERALD_DB_PATH=/path/to/herald.db python3 scripts/digest_dossier.py [digest_id]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import herald_db


def main():
    requested = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = herald_db.get_connection()
    try:
        herald_db.init_schema(conn)
        if requested is None:
            row = conn.execute("SELECT id FROM digests ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                print("no digests found", file=sys.stderr)
                return 1
            requested = row["id"]
        dossier = herald_db.get_digest_dossier(conn, requested)
    finally:
        conn.close()
    if dossier is None:
        print(f"digest {requested} has no dossier (it predates the restored pipeline)", file=sys.stderr)
        return 1
    print(json.dumps(dossier, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
