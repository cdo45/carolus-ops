"""Re-run staging -> canonical transforms. NO QBO calls.

Usage:
    uv run python -m sync.retransform --realm <realm_id>

This is the raw-then-canonical payoff: after a mapping improvement (new
tax handling, job-tag coverage) the canonical layer rebuilds from the
staged payloads already on hand. Idempotent like every transform —
unchanged data writes zero rows — and transactions that now build CLEAN
auto-resolve their open transform_warning flags
(resolution_note 'repaired by re-transform on <date>').
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from sync.transforms import transform_client


def run_retransform(conn: psycopg.Connection, client_id: UUID) -> dict[str, Any]:
    run_row = conn.execute(
        "INSERT INTO runs (client_id, routine) VALUES (%s, 'retransform')"
        " RETURNING id",
        (client_id,),
    ).fetchone()
    assert run_row is not None
    run_id: UUID = run_row[0]
    conn.commit()
    try:
        result = transform_client(conn, client_id)
        summary: dict[str, Any] = {
            "written": result.written,
            "flags_created": result.flags_created,
            "repaired": result.repaired,
            # zero-repair runs explain themselves: what's still wrong & why
            "warnings_retained": result.warnings_retained,
        }
        conn.execute(
            "UPDATE runs SET finished_at = now(), status = 'succeeded',"
            " actions = %s WHERE id = %s",
            (Jsonb(summary), run_id),
        )
        conn.commit()
        return summary
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE runs SET finished_at = now(), status = 'failed',"
            " actions = %s WHERE id = %s",
            (Jsonb({"error": type(exc).__name__}), run_id),
        )
        conn.commit()
        raise


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Re-transform canonical data from staging (no QBO calls)"
    )
    parser.add_argument("--realm", required=True, help="QBO realm id")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id, name FROM clients WHERE qbo_realm_id = %s",
            (args.realm,),
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 1
        client_id, name = row
        summary = run_retransform(conn, client_id)
    print(f"retransform complete: {name}")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
