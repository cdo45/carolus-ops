"""Review-queue lifecycle: idempotent enqueue, resolve, the cross-client inbox.

(RLS coverage for review_queue is asserted by
test_every_client_scoped_table_has_tenant_rls, not here.)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from routines.queue import enqueue, open_items, resolve
from tests.conftest import make_client


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
