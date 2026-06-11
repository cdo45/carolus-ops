"""Fire + non-fire cases for vendor & flow rules R020–R024."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules import (
    r020_vendor_spend_spike,
    r021_new_vendor_large,
    r022_payment_without_bill,
    r023_ar_concentration,
    r024_missing_vendor_on_spend,
)
from rules.r020_vendor_spend_spike import months_back
from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account, make_entity, make_txn

AS_OF = date(2026, 6, 10)


def refs(findings: list) -> set[str]:
    return {finding.source_ref for finding in findings}


def setup_books(conn: psycopg.Connection, client_id: UUID) -> dict[str, UUID]:
    return {
        "bank": make_account(conn, client_id, name="Checking", acct_type="Bank"),
        "expense": make_account(conn, client_id, name="Materials",
                                acct_type="Cost of Goods Sold"),
    }


def test_months_back() -> None:
    assert months_back(date(2026, 6, 10), 6) == date(2025, 12, 1)
    assert months_back(date(2026, 1, 31), 1) == date(2025, 12, 1)


def test_r020_vendor_spend_spike(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    spiky = make_entity(conn, client_id, kind="vendor", name="Spiky Vendor")
    steady = make_entity(conn, client_id, kind="vendor", name="Steady Vendor")
    small = make_entity(conn, client_id, kind="vendor", name="Small Vendor")
    sparse = make_entity(conn, client_id, kind="vendor", name="Sparse Vendor")

    # fires: $100/month across 3 trailing months, then $6,000 this month
    for month in (2, 3, 4):
        balanced_purchase(conn, client_id, amount="100.00",
                          txn_date=date(2026, month, 10), entity_id=spiky,
                          **books)
    balanced_purchase(conn, client_id, amount="6000.00",
                      txn_date=date(2026, 6, 5), entity_id=spiky, **books)
    # non-fire: $3,000/month steady, $6,000 this month (< 3x average and
    # also a legitimate busy month)
    for month in (12, 1, 2, 3, 4, 5):
        year = 2025 if month == 12 else 2026
        balanced_purchase(conn, client_id, amount="3000.00",
                          txn_date=date(year, month, 15), entity_id=steady,
                          **books)
    balanced_purchase(conn, client_id, amount="6000.00",
                      txn_date=date(2026, 6, 5), entity_id=steady, **books)
    # non-fire: huge ratio but under the $5,000 floor
    balanced_purchase(conn, client_id, amount="50.00",
                      txn_date=date(2026, 2, 10), entity_id=small, **books)
    balanced_purchase(conn, client_id, amount="2000.00",
                      txn_date=date(2026, 6, 5), entity_id=small, **books)
    # non-fire: only 2 trailing active months — no baseline to spike against
    for month in (3, 4):
        balanced_purchase(conn, client_id, amount="100.00",
                          txn_date=date(2026, month, 12), entity_id=sparse,
                          **books)
    balanced_purchase(conn, client_id, amount="6000.00",
                      txn_date=date(2026, 6, 5), entity_id=sparse, **books)
    conn.commit()

    findings = r020_vendor_spend_spike.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(spiky)}
    assert findings[0].detail["month_spend"] == "6000.00"
    assert findings[0].detail["trailing_6mo_total"] == "300.00"
    assert findings[0].detail["active_months"] == 3


def test_r021_new_vendor_large(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    big_opener = make_entity(conn, client_id, kind="vendor", name="Big Opener")
    ramped = make_entity(conn, client_id, kind="vendor", name="Ramped Up")
    modest = make_entity(conn, client_id, kind="vendor", name="Modest Start")

    fires = balanced_purchase(conn, client_id, amount="6000.00",
                              txn_type="Bill", txn_date=date(2026, 5, 1),
                              entity_id=big_opener, **books)
    # non-fire: started small, got big later — not a NEW vendor anymore
    balanced_purchase(conn, client_id, amount="300.00",
                      txn_date=date(2026, 4, 1), entity_id=ramped, **books)
    balanced_purchase(conn, client_id, amount="7000.00",
                      txn_date=date(2026, 5, 20), entity_id=ramped, **books)
    # non-fire: first purchase under the threshold
    balanced_purchase(conn, client_id, amount="4999.00",
                      txn_date=date(2026, 5, 1), entity_id=modest, **books)
    conn.commit()

    findings = r021_new_vendor_large.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["vendor"] == "Big Opener"


def test_r022_payment_without_bill(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    vendor = make_entity(conn, client_id, kind="vendor", name="Acme Supply")

    fires = balanced_purchase(conn, client_id, amount="250.00",
                              txn_type="BillPayment",
                              txn_date=date(2026, 6, 1), entity_id=vendor,
                              has_linked_txn=False, **books)
    balanced_purchase(conn, client_id, amount="250.00", txn_type="BillPayment",
                      txn_date=date(2026, 6, 2), entity_id=vendor,
                      has_linked_txn=True, **books)  # non-fire: applied
    balanced_purchase(conn, client_id, amount="250.00", txn_type="Purchase",
                      txn_date=date(2026, 6, 3), entity_id=vendor,
                      has_linked_txn=False, **books)  # non-fire: not a BillPayment
    conn.commit()

    findings = r022_payment_without_bill.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert r022_payment_without_bill.severity == "info"


def ar_invoice(
    conn: psycopg.Connection, client_id: UUID, ar: UUID, income: UUID,
    customer: UUID, amount: str,
) -> UUID:
    return make_txn(
        conn, client_id, txn_type="Invoice", txn_date=date(2026, 5, 10),
        entity_id=customer, amount=amount,
        lines=[{"account": ar, "amount": amount, "posting": "debit"},
               {"account": income, "amount": amount, "posting": "credit"}],
    )


def test_r023_ar_concentration(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    ar = make_account(conn, client_id, name="Accounts Receivable",
                      acct_type="Accounts Receivable")
    income = make_account(conn, client_id, name="Income", acct_type="Income")
    whale = make_entity(conn, client_id, kind="customer", name="Whale Corp")
    minnow = make_entity(conn, client_id, kind="customer", name="Minnow LLC")

    ar_invoice(conn, client_id, ar, income, whale, "30000.00")
    ar_invoice(conn, client_id, ar, income, minnow, "10000.00")
    conn.commit()

    findings = r023_ar_concentration.run(conn, client_id, AS_OF)
    assert refs(findings) == {str(whale)}  # 75% share and > $25k
    assert findings[0].detail["share_pct"] == "75.0"
    assert r023_ar_concentration.severity == "info", (
        "audit: concentration is advisory context, not a close defect"
    )


def test_r023_non_fire_below_floor(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    ar = make_account(conn, client_id, name="Accounts Receivable",
                      acct_type="Accounts Receivable")
    income = make_account(conn, client_id, name="Income", acct_type="Income")
    bank = make_account(conn, client_id, name="Bank", acct_type="Bank")
    big_share = make_entity(conn, client_id, kind="customer", name="Big Share")
    other = make_entity(conn, client_id, kind="customer", name="Other")

    # 83% share but only $24,000 — under the $25k floor; payments reduce AR
    ar_invoice(conn, client_id, ar, income, big_share, "30000.00")
    make_txn(conn, client_id, txn_type="Payment", txn_date=date(2026, 5, 20),
             entity_id=big_share, amount="6000.00",
             lines=[{"account": bank, "amount": "6000.00", "posting": "debit"},
                    {"account": ar, "amount": "6000.00", "posting": "credit"}])
    ar_invoice(conn, client_id, ar, income, other, "5000.00")
    conn.commit()

    assert r023_ar_concentration.run(conn, client_id, AS_OF) == []


def test_r024_missing_vendor_on_spend(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    vendor = make_entity(conn, client_id, kind="vendor", name="Named Vendor")

    fires_purchase = balanced_purchase(conn, client_id, amount="800.00",
                                       txn_date=date(2026, 6, 1),
                                       entity_id=None, **books)
    fires_bill = balanced_purchase(conn, client_id, amount="600.00",
                                   txn_type="Bill", txn_date=date(2026, 6, 2),
                                   entity_id=None, **books)
    balanced_purchase(conn, client_id, amount="400.00",  # non-fire: under $500
                      txn_date=date(2026, 6, 3), entity_id=None, **books)
    balanced_purchase(conn, client_id, amount="800.00",  # non-fire: has vendor
                      txn_date=date(2026, 6, 4), entity_id=vendor, **books)
    conn.commit()

    findings = r024_missing_vendor_on_spend.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires_purchase), str(fires_bill)}


def test_r024_employee_payee_exempt(conn: psycopg.Connection) -> None:
    """Employees aren't synced until P8 — a Purchase whose raw EntityRef
    names an Employee has a payee in QBO, just not in canonical."""
    from psycopg.types.json import Jsonb

    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    reimbursement = balanced_purchase(conn, client_id, amount="800.00",
                                      txn_date=date(2026, 6, 1),
                                      entity_id=None, **books)
    qbo_id = conn.execute(
        "SELECT qbo_id FROM transactions WHERE id = %s", (reimbursement,)
    ).fetchone()
    assert qbo_id is not None
    conn.execute(
        """
        INSERT INTO qbo_raw (client_id, entity_type, qbo_id, payload)
        VALUES (%s, 'Purchase', %s, %s)
        """,
        (client_id, qbo_id[0],
         Jsonb({"Id": qbo_id[0],
                "EntityRef": {"value": "55", "type": "Employee"}})),
    )
    conn.commit()

    assert r024_missing_vendor_on_spend.run(conn, client_id, AS_OF) == []
