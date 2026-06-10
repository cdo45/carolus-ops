"""Fire + non-fire cases for the core ledger rules R010–R017.

All against factory-seeded canonical rows on the scratch DB. as_of is
fixed (2026-06-10, a Wednesday) so date arithmetic is deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import psycopg

from rules import (
    r010_duplicate_payment,
    r011_duplicate_bill,
    r012_round_number_je,
    r013_suspense_aging,
    r014_negative_expense_balance,
    r015_stale_uncategorized,
    r016_backdated_entry,
    r017_weekend_je,
)
from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account, make_entity, make_txn

AS_OF = date(2026, 6, 10)  # Wednesday
SATURDAY = date(2026, 6, 6)
MONDAY = date(2026, 6, 8)


def refs(findings: list) -> set[str]:
    return {finding.source_ref for finding in findings}


def setup_books(conn: psycopg.Connection, client_id: UUID) -> dict[str, UUID]:
    return {
        "bank": make_account(conn, client_id, name="Checking", acct_type="Bank"),
        "expense": make_account(conn, client_id, name="Office", acct_type="Expense"),
        "vendor": make_entity(conn, client_id, kind="vendor", name="Acme Supply"),
    }


def test_r010_duplicate_payment(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    other_vendor = make_entity(conn, client_id, kind="vendor", name="Other Co")

    dup_a = balanced_purchase(  # noqa: F841 - earlier twin, later one is flagged
        conn, client_id, amount="750.00", txn_date=date(2026, 6, 1),
        entity_id=books["vendor"], **_accounts(books),
    )
    dup_b = balanced_purchase(
        conn, client_id, amount="750.00", txn_date=date(2026, 6, 4),
        entity_id=books["vendor"], **_accounts(books),
    )
    # non-fire: small amount (<= $100)
    for day in (1, 3):
        balanced_purchase(conn, client_id, amount="90.00",
                          txn_date=date(2026, 6, day),
                          entity_id=books["vendor"], **_accounts(books))
    # non-fire: same amount but 15 days apart
    balanced_purchase(conn, client_id, amount="400.00",
                      txn_date=date(2026, 5, 1),
                      entity_id=books["vendor"], **_accounts(books))
    balanced_purchase(conn, client_id, amount="400.00",
                      txn_date=date(2026, 5, 16),
                      entity_id=books["vendor"], **_accounts(books))
    # non-fire: different vendors
    balanced_purchase(conn, client_id, amount="600.00",
                      txn_date=date(2026, 6, 2),
                      entity_id=other_vendor, **_accounts(books))
    # non-fire: Bill followed by its BillPayment is normal flow
    balanced_purchase(conn, client_id, amount="900.00", txn_type="Bill",
                      txn_date=date(2026, 6, 1),
                      entity_id=books["vendor"], **_accounts(books))
    balanced_purchase(conn, client_id, amount="900.00", txn_type="BillPayment",
                      txn_date=date(2026, 6, 5),
                      entity_id=books["vendor"], **_accounts(books))
    conn.commit()

    findings = r010_duplicate_payment.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(dup_b)}, "only the later twin is flagged"
    assert findings[0].detail["matches"], "detail names the earlier transaction"


def _accounts(books: dict[str, UUID]) -> dict[str, UUID]:
    return {"bank": books["bank"], "expense": books["expense"]}


def test_r011_duplicate_bill(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    other_vendor = make_entity(conn, client_id, kind="vendor", name="Other Co")

    bill_kwargs = dict(
        txn_type="Bill", entity_id=books["vendor"],
        lines=[{"account": books["expense"], "amount": "100.00"}],
    )
    dup1 = make_txn(conn, client_id, doc_number="INV-9", amount="100.00",
                    txn_date=date(2026, 5, 1), **bill_kwargs)
    dup2 = make_txn(conn, client_id, doc_number="INV-9", amount="105.00",
                    txn_date=date(2026, 5, 20), **bill_kwargs)
    # non-fire: same doc number, different vendor
    make_txn(conn, client_id, doc_number="INV-9", amount="100.00",
             txn_type="Bill", entity_id=other_vendor,
             lines=[{"account": books["expense"], "amount": "100.00"}])
    # non-fire: no doc number
    make_txn(conn, client_id, doc_number=None, amount="100.00", **bill_kwargs)
    make_txn(conn, client_id, doc_number=None, amount="100.00", **bill_kwargs)
    conn.commit()

    findings = r011_duplicate_bill.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(dup1), str(dup2)}, "every group member flagged"
    assert all(f.detail["doc_number"] == "INV-9" for f in findings)


def test_r012_round_number_je(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)

    fires = make_txn(
        conn, client_id, txn_type="JournalEntry", txn_date=MONDAY,
        lines=[
            {"account": books["expense"], "amount": "5000.00", "posting": "debit"},
            {"account": books["bank"], "amount": "5000.00", "posting": "credit"},
        ],
    )
    # non-fire: not a multiple of 1000 / under 1000 / not a JE
    make_txn(conn, client_id, txn_type="JournalEntry", txn_date=MONDAY,
             lines=[
                 {"account": books["expense"], "amount": "1500.00"},
                 {"account": books["bank"], "amount": "1500.00",
                  "posting": "credit"},
             ])
    make_txn(conn, client_id, txn_type="JournalEntry", txn_date=MONDAY,
             lines=[
                 {"account": books["expense"], "amount": "900.00"},
                 {"account": books["bank"], "amount": "900.00",
                  "posting": "credit"},
             ])
    balanced_purchase(conn, client_id, amount="5000.00", txn_date=MONDAY,
                      **_accounts(books))
    conn.commit()

    findings = r012_round_number_je.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["round_amounts"] == ["5000.00", "5000.00"]


def test_r013_suspense_aging(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    suspense = make_account(conn, client_id, name="Ask My Accountant",
                            acct_type="Other Expense")
    cleared = make_account(conn, client_id, name="Clearing",
                           acct_type="Other Current Asset")

    old = AS_OF - timedelta(days=40)
    recent = AS_OF - timedelta(days=10)
    # fires: aged balance parked 40 days ago
    balanced_purchase(conn, client_id, amount="100.00", txn_date=old,
                      bank=books["bank"], expense=suspense)
    # non-fire: clearing account nets to zero across two old postings
    make_txn(conn, client_id, txn_date=old, amount="50.00",
             lines=[{"account": cleared, "amount": "50.00", "posting": "debit"},
                    {"account": books["bank"], "amount": "50.00",
                     "posting": "credit"}])
    make_txn(conn, client_id, txn_date=old + timedelta(days=2), amount="50.00",
             lines=[{"account": cleared, "amount": "50.00", "posting": "credit"},
                    {"account": books["bank"], "amount": "50.00",
                     "posting": "debit"}])
    # non-fire: recent suspense activity only
    suspense2 = make_account(conn, client_id, name="Suspense",
                             acct_type="Other Expense")
    balanced_purchase(conn, client_id, amount="75.00", txn_date=recent,
                      bank=books["bank"], expense=suspense2)
    # non-fire: aged balance in a normally-named account
    balanced_purchase(conn, client_id, amount="80.00", txn_date=old,
                      **_accounts(books))
    conn.commit()

    findings = r013_suspense_aging.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(suspense)}
    assert findings[0].detail["aged_net"] == "100.00"


def test_r014_negative_expense_balance(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    refunded = make_account(conn, client_id, name="Software Subs",
                            acct_type="Expense")
    in_month = date(2026, 6, 5)

    # fires: refund credited to expense this month with no offsetting debit
    make_txn(conn, client_id, txn_date=in_month, amount="300.00",
             lines=[{"account": refunded, "amount": "300.00", "posting": "credit"},
                    {"account": books["bank"], "amount": "300.00",
                     "posting": "debit"}])
    # non-fire: normal net-debit expense this month
    balanced_purchase(conn, client_id, amount="200.00", txn_date=in_month,
                      **_accounts(books))
    # non-fire: credit balance, but in MAY (outside the as_of month)
    last_month = make_account(conn, client_id, name="Travel", acct_type="Expense")
    make_txn(conn, client_id, txn_date=date(2026, 5, 20), amount="150.00",
             lines=[{"account": last_month, "amount": "150.00",
                     "posting": "credit"},
                    {"account": books["bank"], "amount": "150.00",
                     "posting": "debit"}])
    conn.commit()

    findings = r014_negative_expense_balance.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(refunded)}
    assert findings[0].detail["net_credit"] == "300.00"
    assert findings[0].detail["period_start"] == "2026-06-01"


def test_r015_stale_uncategorized(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    uncategorized = make_account(conn, client_id, name="Uncategorized Expense",
                                 acct_type="Expense")

    fires = balanced_purchase(conn, client_id, amount="317.00",
                              txn_date=AS_OF - timedelta(days=20),
                              bank=books["bank"], expense=uncategorized)
    # non-fire: uncategorized but only 5 days old
    balanced_purchase(conn, client_id, amount="50.00",
                      txn_date=AS_OF - timedelta(days=5),
                      bank=books["bank"], expense=uncategorized)
    # non-fire: old but properly categorized
    balanced_purchase(conn, client_id, amount="75.00",
                      txn_date=AS_OF - timedelta(days=20), **_accounts(books))
    conn.commit()

    findings = r015_stale_uncategorized.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["uncategorized_accounts"] == ["Uncategorized Expense"]


def test_r016_backdated_entry(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    txn_date = date(2026, 4, 1)

    fires = balanced_purchase(
        conn, client_id, amount="120.00", txn_date=txn_date, **_accounts(books),
    )
    conn.execute(
        "UPDATE transactions SET qbo_created_at = %s WHERE id = %s",
        (datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc), fires),  # 61 days late
    )
    ok = balanced_purchase(
        conn, client_id, amount="120.00", txn_date=txn_date, **_accounts(books),
    )
    conn.execute(
        "UPDATE transactions SET qbo_created_at = %s WHERE id = %s",
        (datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc), ok),  # 10 days late
    )
    balanced_purchase(  # non-fire: no CreateTime metadata at all
        conn, client_id, amount="120.00", txn_date=txn_date, **_accounts(books),
    )
    conn.commit()

    findings = r016_backdated_entry.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["days_late"] == 61


def test_r017_weekend_je(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)

    je = dict(
        txn_type="JournalEntry",
        lines=[{"account": books["expense"], "amount": "10.00"},
               {"account": books["bank"], "amount": "10.00",
                "posting": "credit"}],
    )
    fires = make_txn(conn, client_id, txn_date=SATURDAY, **je)
    make_txn(conn, client_id, txn_date=MONDAY, **je)  # non-fire: weekday JE
    balanced_purchase(conn, client_id, amount="10.00", txn_date=SATURDAY,
                      **_accounts(books))  # non-fire: weekend but not a JE
    conn.commit()

    findings = r017_weekend_je.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["day"] == "Saturday"
    assert r017_weekend_je.severity == "info"
