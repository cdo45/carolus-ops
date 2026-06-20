"""Review-queue lifecycle: idempotent enqueue, resolve, the cross-client inbox.

(RLS coverage for review_queue is asserted by
test_every_client_scoped_table_has_tenant_rls, not here.)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from routines.flag_queue import project_flags
from routines.queue import enqueue, open_items, resolve, triage
from tests.conftest import make_client


def _open_flag(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    rule_code: str = "R010",
    severity: str = "warn",
    source_ref: str = "t-1",
) -> UUID:
    """An open engine flag (the kind project_flags turns into a queue item)."""
    row = conn.execute(
        "INSERT INTO flags (client_id, rule_code, severity, status, source_type,"
        " source_ref, detail) VALUES (%s, %s, %s, 'open', 'transaction', %s, '{}')"
        " RETURNING id",
        (client_id, rule_code, severity, source_ref),
    ).fetchone()
    assert row is not None
    return row[0]


def _flag_item(conn: psycopg.Connection, flag_id: UUID) -> UUID:
    """The queue item project_flags opened for a flag (source_ref = flag id)."""
    row = conn.execute(
        "SELECT id FROM review_queue WHERE source_type = 'flag' AND source_ref = %s",
        (str(flag_id),),
    ).fetchone()
    assert row is not None
    return row[0]


def _enqueue(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    source_ref: str = "qbo:Bill:1",
    title: str = "Review me",
    priority: str = "info",
    payload: dict[str, Any] | None = None,
) -> UUID:
    return enqueue(
        conn, client_id=client_id, kind="flag", source_type="transaction",
        source_ref=source_ref, title=title, priority=priority, payload=payload,
    )


def _count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM review_queue").fetchone()
    assert row is not None
    return row[0]


def test_enqueue_is_idempotent_on_open_item(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    first = _enqueue(conn, client_id, title="v1", priority="info",
                     payload={"n": 1})
    # same client + source while open → refresh in place, not a duplicate
    second = _enqueue(conn, client_id, title="v2", priority="critical",
                      payload={"n": 2})
    assert second == first
    assert _count(conn) == 1
    row = conn.execute(
        "SELECT title, priority, payload FROM review_queue WHERE id = %s",
        (first,),
    ).fetchone()
    assert row == ("v2", "critical", {"n": 2}), "title/priority/payload refreshed"


def test_resolve_then_reenqueue_opens_a_fresh_item(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    first = _enqueue(conn, client_id)
    resolve(conn, first, status="approved", resolved_by="carlos", note="ok")
    resolved = conn.execute(
        "SELECT status, resolved_by, resolved_at FROM review_queue WHERE id = %s",
        (first,),
    ).fetchone()
    assert resolved is not None
    assert (resolved[0], resolved[1]) == ("approved", "carlos")
    assert resolved[2] is not None

    # the open item is gone, so the same source opens a NEW one
    second = _enqueue(conn, client_id)
    assert second != first
    assert _count(conn) == 2
    assert [item["id"] for item in open_items(conn, client_id)] == [second]


def test_open_items_span_clients_filter_and_order(
    conn: psycopg.Connection,
) -> None:
    client_a = make_client(conn, realm="realm-a")
    client_b = make_client(conn, realm="realm-b")
    _enqueue(conn, client_a, source_ref="a:info", priority="info")
    _enqueue(conn, client_a, source_ref="a:crit", priority="critical")
    _enqueue(conn, client_a, source_ref="a:warn", priority="warn")
    _enqueue(conn, client_b, source_ref="b:1", priority="critical")

    everyone = open_items(conn)
    assert {item["client_id"] for item in everyone} == {client_a, client_b}, (
        "the single queue spans clients"
    )

    only_a = open_items(conn, client_a)
    assert {item["client_id"] for item in only_a} == {client_a}
    assert [item["priority"] for item in only_a] == ["critical", "warn", "info"]


def test_triage_dismiss_closes_item_and_flag(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    flag_id = _open_flag(conn, client_id)
    conn.commit()
    project_flags(conn, client_id)
    item_id = _flag_item(conn, flag_id)

    triage(conn, item_id, action="dismiss", by="carlos")
    conn.commit()

    item = conn.execute(
        "SELECT status, resolved_by FROM review_queue WHERE id = %s", (item_id,)
    ).fetchone()
    assert item == ("dismissed", "carlos"), "the queue item is dismissed"
    flag = conn.execute(
        "SELECT status FROM flags WHERE id = %s", (flag_id,)
    ).fetchone()
    assert flag == ("dismissed",), "the underlying flag is dismissed too"


def test_triage_resolve_closes_item_and_flag_with_manual_note(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    flag_id = _open_flag(conn, client_id)
    conn.commit()
    project_flags(conn, client_id)
    item_id = _flag_item(conn, flag_id)

    triage(conn, item_id, action="resolve", by="carlos")
    conn.commit()

    item = conn.execute(
        "SELECT status FROM review_queue WHERE id = %s", (item_id,)
    ).fetchone()
    assert item == ("resolved",), "the queue item is resolved"
    flag = conn.execute(
        "SELECT status, resolution_note FROM flags WHERE id = %s", (flag_id,)
    ).fetchone()
    assert flag is not None
    status, note = flag
    assert status == "resolved", "the underlying flag is resolved"
    assert note and not note.startswith("condition cleared on "), (
        "a manual resolve note must NOT look like the engine's auto-resolve,"
        " or _reconcile would treat the flag as reopenable"
    )


def test_triage_dismiss_survives_reprojection(conn: psycopg.Connection) -> None:
    """Dismissed-stays-dismissed across a cycle: once triage closes the flag,
    re-running project_flags neither reopens the flag nor re-surfaces an item."""
    client_id = make_client(conn)
    flag_id = _open_flag(conn, client_id)
    conn.commit()
    project_flags(conn, client_id)
    item_id = _flag_item(conn, flag_id)
    triage(conn, item_id, action="dismiss", by="carlos")
    conn.commit()

    project_flags(conn, client_id)  # the nightly projection runs again
    conn.commit()

    flag = conn.execute(
        "SELECT status FROM flags WHERE id = %s", (flag_id,)
    ).fetchone()
    assert flag == ("dismissed",), "the flag stays dismissed"
    reopened = conn.execute(
        "SELECT count(*) FROM review_queue WHERE source_type = 'flag'"
        " AND source_ref = %s AND status = 'open'",
        (str(flag_id),),
    ).fetchone()
    assert reopened == (0,), "no new open queue item re-surfaces for the dismissed flag"
