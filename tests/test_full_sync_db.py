"""DB-backed idempotency test for the full sync (the phase-gate property,
proven against fixtures; the live-sandbox version is tests/gate_phase1.py).

Uses the scratch-database `conn` fixture from conftest.py — skipped unless
CAROLUS_TEST_DB is set; not part of plain CI.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from sync.full_sync import run_full_sync
from tests.conftest import make_client
from tests.qbo_fixtures import FakeQbo

CANONICAL_TABLES = (
    "clients", "accounts", "entities", "jobs", "transactions",
    "journal_lines", "facts", "flags", "kpi_values", "vendor_patterns",
    "documents", "emails",
)


def snapshot(conn: psycopg.Connection) -> dict[str, set[tuple[str, str]]]:
    """Per-table set of (id, xmin): catches inserts, deletes, AND updates."""
    return {
        table: set(
            conn.execute(  # noqa: S608 - table names from a fixed tuple
                f"SELECT id::text, xmin::text FROM {table}"
            ).fetchall()
        )
        for table in CANONICAL_TABLES
    }


def test_full_sync_twice_is_idempotent(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)

    first: dict[str, Any] = run_full_sync(
        conn, client_id, "test-realm-1", qbo=FakeQbo()
    )
    assert first["written"]["accounts"] == 8
    assert first["written"]["entities"] == 3  # 2 customers (1 job) + 1 vendor
    assert first["written"]["jobs"] == 1
    assert first["written"]["transactions"] == 11
    assert first["written"]["journal_lines"] > 0
    assert first["flags_created"] == 1  # invoice 1002's unresolvable item

    before = snapshot(conn)

    second = run_full_sync(conn, client_id, "test-realm-1", qbo=FakeQbo())
    assert second["written"] == {
        "accounts": 0, "entities": 0, "jobs": 0,
        "transactions": 0, "journal_lines": 0,
    }, "second run against unchanged data must write zero canonical rows"
    assert second["flags_created"] == 0, "open flags must not duplicate"

    assert snapshot(conn) == before, "no canonical row may be touched on re-run"

    # staging, by contrast, is an append-only log and is EXPECTED to grow
    rows = conn.execute(
        "SELECT count(DISTINCT (entity_type, qbo_id)), count(*) FROM qbo_raw"
    ).fetchone()
    assert rows is not None
    distinct, total = rows
    assert total == 2 * distinct, "each payload staged once per run"


def test_lines_balance_or_are_flagged(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    run_full_sync(conn, client_id, "test-realm-1", qbo=FakeQbo())

    rows = conn.execute(
        """
        SELECT t.txn_type, t.qbo_id,
               COALESCE(SUM(CASE WHEN jl.posting_type = 'debit'
                                 THEN jl.amount ELSE -jl.amount END), 0) AS net,
               EXISTS (
                   SELECT 1 FROM flags f
                   WHERE f.client_id = t.client_id
                     AND f.rule_code = 'transform_warning'
                     AND f.source_ref = 'qbo:' || t.txn_type || ':' || t.qbo_id
                     AND f.status = 'open'
               ) AS flagged
        FROM transactions t
        LEFT JOIN journal_lines jl ON jl.transaction_id = t.id
        GROUP BY t.id, t.txn_type, t.qbo_id, t.client_id
        """,
    ).fetchall()
    assert len(rows) == 11
    for txn_type, qbo_id, net, flagged in rows:
        assert net == Decimal("0") or flagged, (
            f"{txn_type} {qbo_id}: net {net} with no transform_warning flag"
        )
    unbalanced = [r for r in rows if r[2] != Decimal("0")]
    assert [(r[0], r[1]) for r in unbalanced] == [("Invoice", "1002")]


def test_provenance_fields_extracted(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    run_full_sync(conn, client_id, "test-realm-1", qbo=FakeQbo())

    row = conn.execute(
        """
        SELECT doc_number, qbo_created_at, qbo_synced_at FROM transactions
        WHERE client_id = %s AND qbo_id = '2001' AND txn_type = 'Bill'
        """,
        (client_id,),
    ).fetchone()
    assert row is not None
    doc_number, created_at, synced_at = row
    assert doc_number == "INV-778"
    assert created_at is not None and created_at < synced_at

    linked = {
        (qbo_id, txn_type): has_linked
        for qbo_id, txn_type, has_linked in conn.execute(
            "SELECT qbo_id, txn_type, has_linked_txn FROM transactions"
            " WHERE client_id = %s AND txn_type IN ('Payment', 'BillPayment')",
            (client_id,),
        ).fetchall()
    }
    assert linked[("4001", "BillPayment")] is True, "linked to bill 2001"
    assert linked[("3001", "Payment")] is False, "unapplied customer payment"


def test_job_linkage_and_attribution(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    run_full_sync(conn, client_id, "test-realm-1", qbo=FakeQbo())

    job = conn.execute(
        """
        SELECT j.id, e.qbo_id
        FROM jobs j JOIN entities e ON e.id = j.entity_id
        WHERE j.client_id = %s AND j.qbo_id = '201'
        """,
        (client_id,),
    ).fetchone()
    assert job is not None and job[1] == "200", "job links to parent customer"

    attributed = conn.execute(
        """
        SELECT count(*) FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s AND jl.job_id = %s
        """,
        (client_id, job[0]),
    ).fetchone()
    assert attributed is not None
    # invoice 1001 (3 lines, header job) + bill 2001 materials line
    assert attributed[0] == 4
