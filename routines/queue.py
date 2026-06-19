"""The review queue: the single operator inbox (principle 3, "the queue is
the job"). Thin, deterministic lifecycle over review_queue — no LLM.

enqueue is idempotent on the open item: re-running a producer refreshes the
proposed action in place (the partial unique index keys one open row per
client+source) rather than piling up duplicates. resolve moves an item out
of 'open'; a later enqueue for the same source then opens a fresh item.
Callers own the transaction (these helpers do not commit).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# operator inbox order: act on the worst first, oldest-first within a tier
_PRIORITY_ORDER = "array_position(ARRAY['critical','warn','info'], priority)"


def enqueue(
    conn: psycopg.Connection,
    *,
    client_id: UUID,
    kind: str,
    source_type: str,
    source_ref: str,
    title: str,
    payload: dict[str, Any] | None = None,
    priority: str = "info",
    run_id: UUID | None = None,
) -> UUID:
    """Open (or refresh the open) queue item for one object; return its id."""
    row = conn.execute(
        """
        INSERT INTO review_queue (client_id, kind, priority, source_type,
                                  source_ref, title, payload, run_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, source_type, source_ref) WHERE status = 'open'
        DO UPDATE SET title = excluded.title, payload = excluded.payload,
                      priority = excluded.priority, run_id = excluded.run_id
        RETURNING id
        """,
        (client_id, kind, priority, source_type, source_ref, title,
         Jsonb(payload or {}), run_id),
    ).fetchone()
    assert row is not None
    return row[0]


def resolve(
    conn: psycopg.Connection,
    item_id: UUID,
    *,
    status: str,
    resolved_by: str,
    note: str | None = None,
) -> None:
    """Move an item out of 'open' (approved/dismissed/snoozed), recording who."""
    conn.execute(
        """
        UPDATE review_queue
        SET status = %s, resolved_at = now(), resolved_by = %s,
            resolution_note = %s
        WHERE id = %s
        """,
        (status, resolved_by, note, item_id),
    )


def open_items(
    conn: psycopg.Connection, client_id: UUID | None = None
) -> list[dict[str, Any]]:
    """Open items — the whole queue, or one client — worst priority first."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT * FROM review_queue
            WHERE status = 'open' AND (%s::uuid IS NULL OR client_id = %s)
            ORDER BY {_PRIORITY_ORDER}, created_at
            """,
            (client_id, client_id),
        ).fetchall()
