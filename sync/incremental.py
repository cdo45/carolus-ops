"""Incremental sync via the QBO Change Data Capture endpoint.

Usage:
    uv run python -m sync.incremental --realm <realm_id>

Reads from last_cdc_cursor (falling back to last_full_sync for the first
incremental run), lands every change in qbo_raw untouched, re-transforms,
applies deletions as soft flags, and advances the cursor ONLY when the
whole run succeeded — a failed run re-reads the same window next time.

The changedSince request subtracts a small overlap window from the cursor;
duplicate payloads are free because all canonical writes are idempotent.
CDC deletion stubs (status=Deleted) soft-flag the canonical row
(qbo_deleted_at, first observation wins) and open a 'qbo_deleted' flag —
canonical rows are never hard-deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from sync.full_sync import stage_payloads
from sync.qbo_client import QboClient
from sync.transforms import (
    ALL_ENTITIES,
    TRANSACTION_ENTITIES,
    _flag_once,
    _qbo_last_updated,
    transform_client,
)

CDC_OVERLAP: timedelta = timedelta(minutes=5)
# QBO rejects CDC requests older than 30 days; stay clear of the edge.
CDC_MAX_AGE: timedelta = timedelta(days=29)
QBO_DELETED = "qbo_deleted"


class NoCursor(Exception):
    """Neither last_cdc_cursor nor last_full_sync is set — run full_sync."""


class CursorTooOld(Exception):
    """Cursor is beyond the CDC window — run full_sync to re-baseline."""


@dataclass(frozen=True)
class Deletion:
    entity_type: str
    qbo_id: str
    deleted_at: datetime | None


@dataclass
class CdcChanges:
    upserts: dict[str, list[dict[str, Any]]]
    deletions: list[Deletion]

    @property
    def upsert_count(self) -> int:
        return sum(len(v) for v in self.upserts.values())


def parse_cdc_response(data: dict[str, Any]) -> CdcChanges:
    """Split a CDC response into upsert payloads and deletion stubs."""
    upserts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    deletions: list[Deletion] = []
    for block in data.get("CDCResponse", []):
        for query_response in block.get("QueryResponse", []):
            for key, value in query_response.items():
                if key not in ALL_ENTITIES or not isinstance(value, list):
                    continue
                for payload in value:
                    if payload.get("status") == "Deleted":
                        deletions.append(
                            Deletion(
                                entity_type=key,
                                qbo_id=str(payload["Id"]),
                                deleted_at=_qbo_last_updated(payload),
                            )
                        )
                    else:
                        upserts[key].append(payload)
    return CdcChanges(upserts=dict(upserts), deletions=deletions)


def apply_deletions(
    conn: psycopg.Connection, client_id: UUID, deletions: list[Deletion]
) -> int:
    """Soft-flag deleted canonical rows; never hard delete.

    qbo_deleted_at is set only when NULL (first observation wins) so
    re-processing the same CDC window writes zero rows.
    """
    marked = 0
    for deletion in deletions:
        deleted_at = deletion.deleted_at or datetime.now(timezone.utc)
        if deletion.entity_type in TRANSACTION_ENTITIES:
            cur = conn.execute(
                """
                UPDATE transactions SET qbo_deleted_at = %s
                WHERE client_id = %s AND qbo_id = %s AND txn_type = %s
                  AND qbo_deleted_at IS NULL
                """,
                (deleted_at, client_id, deletion.qbo_id, deletion.entity_type),
            )
        elif deletion.entity_type == "Account":
            cur = conn.execute(
                """
                UPDATE accounts SET qbo_deleted_at = %s
                WHERE client_id = %s AND qbo_id = %s AND qbo_deleted_at IS NULL
                """,
                (deleted_at, client_id, deletion.qbo_id),
            )
        elif deletion.entity_type in ("Customer", "Vendor"):
            kind = deletion.entity_type.lower()
            cur = conn.execute(
                """
                UPDATE entities SET qbo_deleted_at = %s
                WHERE client_id = %s AND qbo_id = %s AND kind = %s
                  AND qbo_deleted_at IS NULL
                """,
                (deleted_at, client_id, deletion.qbo_id, kind),
            )
            conn.execute(
                """
                UPDATE jobs SET qbo_deleted_at = %s
                WHERE client_id = %s AND qbo_id = %s AND qbo_deleted_at IS NULL
                """,
                (deleted_at, client_id, deletion.qbo_id),
            )
        else:
            continue  # Item and friends: staging-only, nothing canonical to mark
        if cur.rowcount:
            marked += cur.rowcount
            _flag_once(
                conn,
                client_id,
                QBO_DELETED,
                "qbo_cdc",
                f"qbo:{deletion.entity_type}:{deletion.qbo_id}",
                f"{deletion.entity_type} {deletion.qbo_id} deleted in QBO"
                " — canonical row soft-flagged, review impact",
            )
    return marked


def run_incremental_sync(
    conn: psycopg.Connection,
    client_id: UUID,
    realm_id: str,
    *,
    qbo: QboClient | None = None,
    overlap: timedelta = CDC_OVERLAP,
) -> dict[str, Any]:
    """CDC-sync one client; returns the summary stored on the run row."""
    qbo = qbo or QboClient(conn, client_id, realm_id)
    state = conn.execute(
        "SELECT last_cdc_cursor, last_full_sync FROM sync_connections"
        " WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    cursor: datetime | None = (state[0] or state[1]) if state else None
    if cursor is None:
        raise NoCursor(
            f"client {client_id}: no CDC cursor and no full sync — run"
            " `uv run python -m sync.full_sync` first"
        )
    now = datetime.now(timezone.utc)
    if cursor < now - CDC_MAX_AGE:
        raise CursorTooOld(
            f"client {client_id}: cursor {cursor.isoformat()} is outside the"
            " CDC window — run full_sync to re-baseline"
        )

    run_row = conn.execute(
        "INSERT INTO runs (client_id, routine) VALUES (%s, 'incremental_sync')"
        " RETURNING id",
        (client_id,),
    ).fetchone()
    assert run_row is not None
    run_id: UUID = run_row[0]
    conn.commit()

    try:
        changed_since = cursor - overlap
        data = qbo.cdc(list(ALL_ENTITIES), changed_since)
        changes = parse_cdc_response(data)

        staged: dict[str, int] = {}
        for entity_type, payloads in changes.upserts.items():
            staged[entity_type] = stage_payloads(
                conn, client_id, entity_type, payloads, run_id
            )
        for deletion in changes.deletions:
            conn.execute(
                """
                INSERT INTO qbo_raw
                    (client_id, entity_type, qbo_id, payload, sync_run_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    client_id,
                    deletion.entity_type,
                    deletion.qbo_id,
                    Jsonb({"Id": deletion.qbo_id, "status": "Deleted"}),
                    run_id,
                ),
            )
        conn.commit()

        result = transform_client(conn, client_id)
        deleted = apply_deletions(conn, client_id, changes.deletions)

        response_time = data.get("time")
        next_cursor = (
            datetime.fromisoformat(response_time) if response_time else now
        )
        summary: dict[str, Any] = {
            "changed_since": changed_since.isoformat(),
            "staged": staged,
            "deletions": deleted,
            "written": result.written,
            "flags_created": result.flags_created,
            "next_cursor": next_cursor.isoformat(),
        }
        # cursor advances ONLY here, after every step above succeeded
        conn.execute(
            "UPDATE sync_connections SET last_cdc_cursor = %s WHERE client_id = %s",
            (next_cursor, client_id),
        )
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
    parser = argparse.ArgumentParser(description="Incremental (CDC) QBO sync")
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
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 1
        client_id, name = row
        try:
            summary = run_incremental_sync(conn, client_id, args.realm)
        except (NoCursor, CursorTooOld) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(f"incremental sync complete: realm {args.realm} ({name})")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
