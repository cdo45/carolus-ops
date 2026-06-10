"""Full sync: pull every tracked QBO entity into staging, then transform.

Usage:
    uv run python -m sync.full_sync --realm <realm_id>

Stages are separable on purpose: qbo_raw is an append-only log of exactly
what QBO returned, so transforms can be re-run (or fixed and re-run) without
touching the QBO API. Idempotency: re-running the full sync against
unchanged books appends new staging log rows but writes ZERO canonical rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from sync.qbo_client import QboClient
from sync.transforms import (
    REFERENCE_ENTITIES,
    TRANSACTION_ENTITIES,
    transform_client,
)


def stage_payloads(
    conn: psycopg.Connection,
    client_id: UUID,
    entity_type: str,
    payloads: Sequence[dict[str, Any]],
    sync_run_id: UUID,
) -> int:
    """Append raw payloads to the staging log, untouched."""
    count = 0
    for payload in payloads:
        conn.execute(
            """
            INSERT INTO qbo_raw (client_id, entity_type, qbo_id, payload, sync_run_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (client_id, entity_type, str(payload["Id"]), Jsonb(payload), sync_run_id),
        )
        count += 1
    return count


def run_full_sync(
    conn: psycopg.Connection,
    client_id: UUID,
    realm_id: str,
    *,
    qbo: QboClient | None = None,
) -> dict[str, Any]:
    """Sync one client end to end; returns the summary stored on the run row."""
    qbo = qbo or QboClient(conn, client_id, realm_id)
    run_row = conn.execute(
        "INSERT INTO runs (client_id, routine) VALUES (%s, 'full_sync') RETURNING id",
        (client_id,),
    ).fetchone()
    assert run_row is not None
    run_id: UUID = run_row[0]
    conn.commit()

    try:
        fetched: dict[str, int] = {}
        for entity_type in REFERENCE_ENTITIES + TRANSACTION_ENTITIES:
            payloads = qbo.query(entity_type)
            fetched[entity_type] = stage_payloads(
                conn, client_id, entity_type, payloads, run_id
            )
        conn.commit()

        result = transform_client(conn, client_id)
        summary: dict[str, Any] = {
            "fetched": fetched,
            "written": result.written,
            "flags_created": result.flags_created,
        }
        conn.execute(
            "UPDATE sync_connections SET last_full_sync = now() WHERE client_id = %s",
            (client_id,),
        )
        conn.execute(
            """
            UPDATE runs SET finished_at = now(), status = 'succeeded', actions = %s
            WHERE id = %s
            """,
            (Jsonb(summary), run_id),
        )
        conn.commit()
        return summary
    except Exception as exc:
        conn.rollback()
        conn.execute(
            """
            UPDATE runs SET finished_at = now(), status = 'failed', actions = %s
            WHERE id = %s
            """,
            (Jsonb({"error": type(exc).__name__}), run_id),
        )
        conn.commit()
        raise


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Full QBO sync for one client")
    parser.add_argument("--realm", required=True, help="QBO realm id")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id, name FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(
                f"no client for realm {args.realm} — run `uv run python -m"
                " sync.connect` first",
                file=sys.stderr,
            )
            return 1
        client_id, name = row
        summary = run_full_sync(conn, client_id, args.realm)

    print(f"full sync complete: realm {args.realm} ({name})")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
