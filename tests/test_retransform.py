"""The repair path: a re-transform from staging that rebuilds a
transaction CLEAN auto-resolves its transform_warning. No QBO calls."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from sync.full_sync import stage_payloads
from sync.retransform import run_retransform
from sync.transforms import transform_client
from tests.conftest import make_client

ACCOUNTS: list[dict[str, Any]] = [
    {"Id": "20", "Name": "Accounts Receivable",
     "AccountType": "Accounts Receivable", "Active": True},
    {"Id": "30", "Name": "Income", "AccountType": "Income", "Active": True},
]
TAX_ACCOUNT = {"Id": "70", "Name": "Sales Tax Payable",
               "AccountType": "Other Current Liability",
               "AccountSubType": "GlobalTaxPayable", "Active": True}
ITEM = {"Id": "100", "Name": "Service", "IncomeAccountRef": {"value": "30"}}
TAXED_INVOICE = {
    "Id": "9101", "TxnDate": "2026-05-05", "TotalAmt": 108.00,
    "Line": [{"Amount": 100.00,
              "SalesItemLineDetail": {"ItemRef": {"value": "100"}}}],
    "TxnTaxDetail": {"TotalTax": 8.00},
    "MetaData": {"LastUpdatedTime": "2026-05-05T10:00:00-07:00"},
}


def stage(conn: psycopg.Connection, client_id: UUID, entity: str,
          payloads: list[dict[str, Any]]) -> None:
    run = conn.execute(
        "INSERT INTO runs (client_id, routine) VALUES (%s, 'test_stage')"
        " RETURNING id",
        (client_id,),
    ).fetchone()
    assert run is not None
    stage_payloads(conn, client_id, entity, payloads, run[0])
    conn.commit()


def warning_flags(conn: psycopg.Connection, client_id: UUID) -> list[tuple]:
    return conn.execute(
        "SELECT status, resolution_note FROM flags WHERE client_id = %s"
        " AND rule_code = 'transform_warning' ORDER BY created_at",
        (client_id,),
    ).fetchall()


def test_repair_resolves_warning_when_staging_heals(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    stage(conn, client_id, "Account", ACCOUNTS)  # NO tax account yet
    stage(conn, client_id, "Item", [ITEM])
    stage(conn, client_id, "Invoice", [TAXED_INVOICE])

    first = transform_client(conn, client_id)
    assert first.flags_created == 1 and first.repaired == 0
    assert warning_flags(conn, client_id) == [("open", None)]

    # the missing liability account arrives in staging; retransform repairs
    stage(conn, client_id, "Account", [TAX_ACCOUNT])
    summary = run_retransform(conn, client_id)

    assert summary["repaired"] == 1 and summary["flags_created"] == 0
    ((status, note),) = warning_flags(conn, client_id)
    assert status == "resolved"
    assert note is not None and note.startswith("repaired by re-transform on ")

    net = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN jl.posting_type = 'debit'
                                 THEN jl.amount ELSE -jl.amount END), 0)
        FROM journal_lines jl JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s AND t.qbo_id = '9101'
        """,
        (client_id,),
    ).fetchone()
    assert net == (0,), "the taxed invoice now balances"

    run_status = conn.execute(
        "SELECT status FROM runs WHERE client_id = %s AND"
        " routine = 'retransform'",
        (client_id,),
    ).fetchall()
    assert run_status == [("succeeded",)]


def test_repair_is_idempotent(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    stage(conn, client_id, "Account", [*ACCOUNTS, TAX_ACCOUNT])
    stage(conn, client_id, "Item", [ITEM])
    stage(conn, client_id, "Invoice", [TAXED_INVOICE])

    first = transform_client(conn, client_id)
    assert first.flags_created == 0, "clean from the start — nothing flagged"

    again = run_retransform(conn, client_id)
    assert again == {
        # keys appear only for entity loops that had staged payloads
        "written": {"accounts": 0, "transactions": 0, "journal_lines": 0},
        "flags_created": 0,
        "repaired": 0,
    }, "re-running over unchanged staging writes and repairs nothing"


def test_repair_does_not_touch_manual_or_foreign_flags(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    stage(conn, client_id, "Account", [*ACCOUNTS, TAX_ACCOUNT])
    stage(conn, client_id, "Item", [ITEM])
    stage(conn, client_id, "Invoice", [TAXED_INVOICE])
    # an unrelated open flag with a different source_ref must survive
    conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status,
                           source_type, source_ref, detail)
        VALUES (%s, 'transform_warning', 'warn', 'open', 'transaction',
                'qbo:Invoice:other', 'still broken elsewhere')
        """,
        (client_id,),
    )
    conn.commit()

    result = transform_client(conn, client_id)

    assert result.repaired == 0, "only the rebuilt txn's own ref is repaired"
    statuses = {ref: status for ref, status in conn.execute(
        "SELECT source_ref, status FROM flags WHERE client_id = %s",
        (client_id,),
    ).fetchall()}
    assert statuses == {"qbo:Invoice:other": "open"}


def test_full_sync_reports_repaired(conn: psycopg.Connection) -> None:
    """full sync surfaces the repair count too (summary plumbing)."""
    from sync.full_sync import run_full_sync
    from tests.qbo_fixtures import FakeQbo

    client_id = make_client(conn)
    summary = run_full_sync(conn, client_id, "test-realm-1", qbo=FakeQbo())
    assert "repaired" in summary and summary["repaired"] == 0
    assert summary["flags_created"] == 1  # invoice 1002's unresolvable item
