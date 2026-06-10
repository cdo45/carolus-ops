"""DB-backed idempotency test for the full sync (the phase-gate property,
proven against fixtures; the live-sandbox version is tests/gate_phase1.py).

Skipped unless CAROLUS_TEST_DB points at a scratch Postgres database —
the schema there is DROPPED and rebuilt every run. Not part of plain CI.

Run locally:
    CAROLUS_TEST_DB=postgresql://carolus:...@localhost:5432/carolus_test \
        uv run pytest tests/test_full_sync_db.py
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest

from db.migrate import migrate
from sync.full_sync import run_full_sync
from tests.qbo_fixtures import FakeQbo

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAROLUS_TEST_DB"),
    reason="CAROLUS_TEST_DB not set (scratch database required)",
)

CANONICAL_TABLES = (
    "clients", "accounts", "entities", "jobs", "transactions",
    "journal_lines", "facts", "flags", "kpi_values", "vendor_patterns",
    "documents", "emails",
)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = os.environ["CAROLUS_TEST_DB"]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    migrate(url)
    with psycopg.connect(url) as connection:
        yield connection


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


def make_client(conn: psycopg.Connection) -> UUID:
    row = conn.execute(
        "INSERT INTO clients (name, qbo_realm_id) VALUES (%s, %s) RETURNING id",
        ("Fixture Co", "test-realm-1"),
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


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
