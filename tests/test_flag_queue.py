"""Projecting rules-engine flags into the review queue.

Flags are inserted directly (as the engine writes them) — the engine is not
exercised here. Covers the projection, idempotent refresh, auto-close on
clear, and that sync's plain-text detail does not crash the projection.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from routines.flag_queue import project_flags
from routines.queue import open_items
from tests.conftest import make_client


def _flag(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    rule_code: str = "R010",
    severity: str = "critical",
    source_ref: str = "t-1",
    status: str = "open",
    detail: Any = None,
) -> UUID:
    """Insert a flag the way the engine does (detail is a JSON string)."""
    row = conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status, source_type,
                           source_ref, detail)
        VALUES (%s, %s, %s, %s, 'transaction', %s, %s) RETURNING id
        """,
        (client_id, rule_code, severity, status, source_ref,
         json.dumps(detail if detail is not None else {"k": "v"})),
    ).fetchone()
    assert row is not None
    return row[0]


def test_projects_one_open_item_per_open_flag(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    critical = _flag(conn, client_id, rule_code="R010", severity="critical",
                     source_ref="t-1", detail={"amount": "750.00"})
    warn = _flag(conn, client_id, rule_code="R013", severity="warn",
                 source_ref="acct-1")
    conn.commit()

    project_flags(conn, client_id)

    items = {item["source_ref"]: item for item in open_items(conn, client_id)}
    assert set(items) == {str(critical), str(warn)}, "one item per open flag"
    item = items[str(critical)]
    assert item["kind"] == "flag"
    assert item["priority"] == "critical", "priority = flag severity"
    assert item["title"] == "R010", "title = rule_code"
    assert item["payload"]["rule_code"] == "R010"
    assert item["payload"]["target_ref"] == "t-1"
    assert item["payload"]["detail"] == {"amount": "750.00"}, "nested, not double-encoded"


def test_idempotent_refreshes_in_place(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    flag_id = _flag(conn, client_id, rule_code="R010", severity="warn",
                    source_ref="t-1")
    conn.commit()

    project_flags(conn, client_id)
    (first,) = open_items(conn, client_id)
    project_flags(conn, client_id)  # second run, flag unchanged
    again = open_items(conn, client_id)
    assert len(again) == 1 and again[0]["id"] == first["id"], "no duplicate"

    # the engine re-runs and the flag's severity rises; re-project refreshes
    conn.execute("UPDATE flags SET severity = 'critical' WHERE id = %s", (flag_id,))
    project_flags(conn, client_id)
    (refreshed,) = open_items(conn, client_id)
    assert refreshed["id"] == first["id"], "same item, not a new one"
    assert refreshed["priority"] == "critical", "refreshed from the flag"
    total = conn.execute("SELECT count(*) FROM review_queue").fetchone()
    assert total == (1,)


def test_auto_closes_item_when_flag_clears(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    clearing = _flag(conn, client_id, rule_code="R010", severity="warn",
                     source_ref="t-1")
    staying = _flag(conn, client_id, rule_code="R013", severity="info",
                    source_ref="acct-1")
    conn.commit()
    project_flags(conn, client_id)
    assert len(open_items(conn, client_id)) == 2

    conn.execute("UPDATE flags SET status = 'resolved' WHERE id = %s", (clearing,))
    project_flags(conn, client_id)

    open_refs = [item["source_ref"] for item in open_items(conn, client_id)]
    assert open_refs == [str(staying)], "the cleared flag's item closed; the open one stays"
    closed = conn.execute(
        "SELECT status, resolved_by, resolution_note FROM review_queue"
        " WHERE source_ref = %s",
        (str(clearing),),
    ).fetchone()
    assert closed == ("dismissed", "system", "underlying flag resolved")


def test_non_json_detail_does_not_crash(conn: psycopg.Connection) -> None:
    """Sync writes plain-text detail (transform_warning, qbo_deleted); the
    projection must wrap it as a string, never crash on the jsonb cast."""
    client_id = make_client(conn)
    row = conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status, source_type,
                           source_ref, detail)
        VALUES (%s, 'transform_warning', 'warn', 'open', 'transaction',
                'qbo:Invoice:1', %s) RETURNING id
        """,
        (client_id, "unbalanced lines: debits 5.00 != credits 4.00"),
    ).fetchone()
    assert row is not None
    conn.commit()

    project_flags(conn, client_id)  # must not raise

    (item,) = open_items(conn, client_id)
    assert item["source_ref"] == str(row[0])
    assert item["payload"]["detail"] == "unbalanced lines: debits 5.00 != credits 4.00"
