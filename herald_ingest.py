#!/usr/bin/env python3
"""
herald_ingest.py — Read JSONL of normalised tweets and write to /var/lib/herald/herald.db.

Idempotent: re-ingesting the same posts is a no-op due to (source, external_id) UNIQUE.
Posts and accounts are upserted; each ingest run is logged to pipeline_runs for the debug log.

Usage:
    python3 herald_ingest.py parsed.jsonl
    python3 herald_parser.py < unified.json | python3 herald_ingest.py
"""
import json
import sys
import time

import herald_db


def main():
    if len(sys.argv) > 1:
        infile = open(sys.argv[1])
    else:
        infile = sys.stdin

    conn = herald_db.get_connection()
    herald_db.init_schema(conn)
    run_id = herald_db.start_pipeline_run(conn, "ingest", source="x")
    started = time.time()
    rows_seen = 0
    errors = []

    try:
        for line in infile:
            line = line.strip()
            if not line:
                continue
            try:
                post = json.loads(line)
                rows_seen += 1
                herald_db.upsert_post(conn, post)
                if post.get("author", {}).get("id"):
                    herald_db.upsert_account(conn, post["author"])
            except Exception as e:
                errors.append({"line_no": rows_seen + 1, "error": str(e)})

        conn.commit()
        herald_db.finish_pipeline_run(conn, run_id, "success", rows_out=rows_seen)
        conn.commit()  # ensure the status update is durable before close

    except Exception as e:
        conn.rollback()
        herald_db.finish_pipeline_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        conn.close()

    elapsed = time.time() - started
    print(
        f"[ingest] seen={rows_seen} errors={len(errors)} elapsed={elapsed:.2f}s db={herald_db.DB_PATH}",
        file=sys.stderr,
    )
    if errors:
        for err in errors[:5]:
            print(f"  {err}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
