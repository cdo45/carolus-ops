"""Fire + non-fire cases for the controller-audit additions:
R018, R025, R026, R027, R028, R034, R035."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from rules import (
    r018_post_close_edit,
    r025_stale_ar,
    r026_ap_aging,
    r027_duplicate_vendors,
    r028_negative_bank,
    r034_untagged_cogs_ratio,
    r035_underbilling_watch,
)
from tests.conftest import make_client
from tests.factories import (
    balanced_purchase,
    make_account,
    make_entity,
    make_job,
    make_txn,
)

AS_OF = date(2026, 6, 10)


def refs(findings: list) -> set[str]:
    return {finding.source_ref for finding in findings}


def stage_balance(
    conn: psycopg.Connection, client_id: UUID, txn_id: UUID, balance: str,
) -> None:
    """Stage a payload carrying QBO's own Balance for an existing txn."""
    row = conn.execute(
        "SELECT qbo_id, txn_type FROM transactions WHERE id = %s", (txn_id,)
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO qbo_raw (client_id, entity_type, qbo_id, payload)"
        " VALUES (%s, %s, %s, %s)",
        (client_id, row[1], row[0],
         Jsonb({"Id": row[0], "Balance": float(balance)})),
    )
    conn.commit()


# ------------------------------------------------------------------ R018


def green_close(
    conn: psycopg.Connection, client_id: UUID, evaluated: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO close_runs (client_id, period_start, period_end, status,
                                conditions, evaluated_at)
        VALUES (%s, '2026-04-01', '2026-04-30', 'green', '[]', %s)
        """,
        (client_id, evaluated),
    )
    conn.commit()


def test_r018_post_close_edit(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")
    closed_at = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    green_close(conn, client_id, closed_at)

    def april_purchase(synced: datetime | None) -> UUID:
        txn = balanced_purchase(conn, client_id, amount="100.00",
                                txn_date=date(2026, 4, 15),
                                bank=bank, expense=expense)
        conn.execute("UPDATE transactions SET qbo_synced_at = %s WHERE id = %s",
                     (synced, txn))
        return txn

    fires = april_purchase(closed_at + timedelta(days=3))  # edited after close
    april_purchase(closed_at - timedelta(days=10))  # non-fire: settled before
    april_purchase(None)  # non-fire: no modification timestamp
    # non-fire: edited after close but dated OUTSIDE the closed period
    outside = balanced_purchase(conn, client_id, amount="100.00",
                                txn_date=date(2026, 5, 15),
                                bank=bank, expense=expense)
    conn.execute("UPDATE transactions SET qbo_synced_at = %s WHERE id = %s",
                 (closed_at + timedelta(days=3), outside))
    conn.commit()

    findings = r018_post_close_edit.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["closed_period"] == "2026-04-01 to 2026-04-30"
    assert r018_post_close_edit.severity == "critical"


def test_r018_silent_without_green_close(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")
    txn = balanced_purchase(conn, client_id, amount="100.00",
                            txn_date=date(2026, 4, 15),
                            bank=bank, expense=expense)
    conn.execute("UPDATE transactions SET qbo_synced_at = now() WHERE id = %s",
                 (txn,))
    # a RED close over the same period must not arm the rule
    conn.execute(
        """
        INSERT INTO close_runs (client_id, period_start, period_end, status,
                                conditions, evaluated_at)
        VALUES (%s, '2026-04-01', '2026-04-30', 'red', '[]',
                '2026-05-05T12:00:00+00:00')
        """,
        (client_id,),
    )
    conn.commit()

    assert r018_post_close_edit.run(conn, client_id, AS_OF) == []


# ------------------------------------------------------------- R025 / R026


def ar_setup(conn: psycopg.Connection, client_id: UUID) -> dict[str, Any]:
    return {
        "ar": make_account(conn, client_id, name="Accounts Receivable",
                           acct_type="Accounts Receivable"),
        "income": make_account(conn, client_id, name="Income",
                               acct_type="Income"),
        "customer": make_entity(conn, client_id, kind="customer",
                                name="Slow Payer Inc"),
    }


def invoice(
    conn: psycopg.Connection, client_id: UUID, books: dict[str, Any],
    amount: str, when: date,
) -> UUID:
    return make_txn(
        conn, client_id, txn_type="Invoice", txn_date=when,
        entity_id=books["customer"], amount=amount,
        lines=[{"account": books["ar"], "amount": amount, "posting": "debit"},
               {"account": books["income"], "amount": amount,
                "posting": "credit"}],
    )


def test_r025_stale_ar(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = ar_setup(conn, client_id)
    old = AS_OF - timedelta(days=100)

    fires = invoice(conn, client_id, books, "1500.00", old)
    stage_balance(conn, client_id, fires, "1500.00")
    paid_off = invoice(conn, client_id, books, "2000.00", old)
    stage_balance(conn, client_id, paid_off, "0")  # non-fire: QBO says paid
    small = invoice(conn, client_id, books, "900.00", old)
    stage_balance(conn, client_id, small, "900.00")  # non-fire: < $1,000
    recent = invoice(conn, client_id, books, "5000.00",
                     AS_OF - timedelta(days=30))
    stage_balance(conn, client_id, recent, "5000.00")  # non-fire: young
    conn.commit()

    findings = r025_stale_ar.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["open_balance"] == "1500.00"


def test_r026_ap_aging(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")
    vendor = make_entity(conn, client_id, kind="vendor", name="Patient Supply")
    old = AS_OF - timedelta(days=70)

    fires = balanced_purchase(conn, client_id, amount="1200.00",
                              txn_type="Bill", txn_date=old,
                              entity_id=vendor, bank=bank, expense=expense)
    stage_balance(conn, client_id, fires, "1200.00")
    paid = balanced_purchase(conn, client_id, amount="3000.00",
                             txn_type="Bill", txn_date=old,
                             entity_id=vendor, bank=bank, expense=expense)
    stage_balance(conn, client_id, paid, "0")  # non-fire: paid per QBO
    recent = balanced_purchase(conn, client_id, amount="1200.00",
                               txn_type="Bill",
                               txn_date=AS_OF - timedelta(days=30),
                               entity_id=vendor, bank=bank, expense=expense)
    stage_balance(conn, client_id, recent, "1200.00")  # non-fire: young
    conn.commit()

    findings = r026_ap_aging.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["vendor"] == "Patient Supply"


# ------------------------------------------------------------------ R027


def test_r027_duplicate_vendors(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    a = make_entity(conn, client_id, kind="vendor",
                    name="Hadley Construction Group")
    b = make_entity(conn, client_id, kind="vendor",
                    name="Hadley Construction Group LLC")  # sim 0.87: fires
    make_entity(conn, client_id, kind="vendor", name="Home Depot")
    make_entity(conn, client_id, kind="vendor",
                name="Home Depot #4821")  # sim 0.69: just under threshold
    make_entity(conn, client_id, kind="vendor", name="Sunbelt Rentals",
                active=False)
    make_entity(conn, client_id, kind="vendor",
                name="Sunbelt Rentals Co")  # similar, but partner inactive
    make_entity(conn, client_id, kind="customer",
                name="Hadley Construction Group Inc")  # cross-kind: excluded
    conn.commit()

    findings = r027_duplicate_vendors.run(conn, client_id, AS_OF)

    anchor = str(min(a, b, key=lambda u: str(u)))
    assert refs(findings) == {anchor}, (
        "one finding per anchor; sub-threshold, inactive, and cross-kind"
        " pairs excluded"
    )
    (finding,) = findings
    partner_names = [p["name"] for p in finding.detail["similar_to"]]
    assert partner_names in (["Hadley Construction Group"],
                             ["Hadley Construction Group LLC"])

    rerun = r027_duplicate_vendors.run(conn, client_id, AS_OF)
    assert refs(rerun) == {anchor}, "stable anchor across runs"


# ------------------------------------------------------------------ R028


def test_r028_negative_bank(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    overdrawn = make_account(conn, client_id, name="Operating",
                             acct_type="Bank")
    healthy = make_account(conn, client_id, name="Savings", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")

    balanced_purchase(conn, client_id, amount="500.00",
                      txn_date=date(2026, 6, 5),
                      bank=overdrawn, expense=expense)  # balance -500
    make_txn(conn, client_id, txn_type="Deposit", txn_date=date(2026, 6, 1),
             amount="2000.00",
             lines=[{"account": healthy, "amount": "2000.00",
                     "posting": "debit"},
                    {"account": expense, "amount": "2000.00",
                     "posting": "credit"}])
    # next-month activity must not affect this month-end measurement
    make_txn(conn, client_id, txn_type="Deposit", txn_date=date(2026, 7, 2),
             amount="900.00",
             lines=[{"account": overdrawn, "amount": "900.00",
                     "posting": "debit"},
                    {"account": expense, "amount": "900.00",
                     "posting": "credit"}])
    conn.commit()

    findings = r028_negative_bank.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(overdrawn)}
    assert findings[0].detail["book_balance"] == "-500.00"
    assert findings[0].detail["as_of_month_end"] == "2026-06-30"


# ------------------------------------------------------------------ R034


def cogs_setup(conn: psycopg.Connection, client_id: UUID) -> dict[str, Any]:
    customer = make_entity(conn, client_id, kind="customer", name="Acme")
    return {
        "bank": make_account(conn, client_id, name="Checking",
                             acct_type="Bank"),
        "cogs": make_account(conn, client_id, name="Job Materials",
                             acct_type="Cost of Goods Sold"),
        "job": make_job(conn, client_id, entity_id=customer, name="Job A"),
    }


def test_r034_untagged_ratio(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = cogs_setup(conn, client_id)
    in_month = date(2026, 6, 3)

    # tagged $9,000; untagged $600 -> 6.25% of $9,600 -> fires
    balanced_purchase(conn, client_id, amount="9000.00", txn_date=in_month,
                      job=books["job"], bank=books["bank"],
                      expense=books["cogs"])
    for amount in ("350.00", "250.00"):  # each under R030's $500 floor
        balanced_purchase(conn, client_id, amount=amount, txn_date=in_month,
                          bank=books["bank"], expense=books["cogs"])
    conn.commit()

    findings = r034_untagged_cogs_ratio.run(conn, client_id, AS_OF)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.source_type == "client"
    assert finding.source_ref == str(client_id)
    assert finding.detail == {
        "month": "2026-06", "untagged_cogs": "600.00",
        "total_cogs": "9600.00", "untagged_pct": "6.3",
    }


def test_r034_non_fire_below_ratio_or_floor(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = cogs_setup(conn, client_id)
    in_month = date(2026, 6, 3)

    # 4% untagged -> under the ratio
    balanced_purchase(conn, client_id, amount="9600.00", txn_date=in_month,
                      job=books["job"], bank=books["bank"],
                      expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="400.00", txn_date=in_month,
                      bank=books["bank"], expense=books["cogs"])
    conn.commit()
    assert r034_untagged_cogs_ratio.run(conn, client_id, AS_OF) == []

    tiny_client = make_client(conn, realm="tiny-month")
    tiny = cogs_setup(conn, tiny_client)
    balanced_purchase(conn, tiny_client, amount="80.00", txn_date=in_month,
                      bank=tiny["bank"], expense=tiny["cogs"])
    conn.commit()
    assert r034_untagged_cogs_ratio.run(conn, tiny_client, AS_OF) == [], (
        "100% untagged but under the $5,000 denominator floor"
    )


# ------------------------------------------------------------------ R035


def test_r035_underbilling_watch(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = cogs_setup(conn, client_id)
    ar = make_account(conn, client_id, name="Accounts Receivable",
                      acct_type="Accounts Receivable")
    income = make_account(conn, client_id, name="Income", acct_type="Income")
    customer = make_entity(conn, client_id, kind="customer", name="Owner LLC")

    def job_with(name: str, costs: list[tuple[str, date]],
                 billed: date | None = None) -> UUID:
        job = make_job(conn, client_id, entity_id=customer, name=name)
        for amount, when in costs:
            balanced_purchase(conn, client_id, amount=amount, txn_date=when,
                              job=job, bank=books["bank"],
                              expense=books["cogs"])
        if billed is not None:
            make_txn(conn, client_id, txn_type="Invoice", txn_date=billed,
                     entity_id=customer, amount="4000.00",
                     lines=[{"account": ar, "amount": "4000.00",
                             "posting": "debit", "job": job},
                            {"account": income, "amount": "4000.00",
                             "posting": "credit", "job": job}])
        return job

    recent, older = AS_OF - timedelta(days=10), AS_OF - timedelta(days=50)
    fires = job_with("Burning Unbilled",
                     [("8000.00", older), ("4000.00", recent)])
    job_with("Billed Recently", [("8000.00", older), ("4000.00", recent)],
             billed=AS_OF - timedelta(days=5))  # non-fire: invoice went out
    job_with("Small Active", [("3000.00", recent)])  # non-fire: < $10k JTD
    job_with("Dormant", [("15000.00", older)])  # non-fire: no recent costs
    conn.commit()

    findings = r035_underbilling_watch.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["jtd_costs"] == "12000.00"
