"""Fire + non-fire cases for construction rules R030–R033."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules import (
    r030_cogs_without_job,
    r031_job_cost_after_completion,
    r032_job_margin_negative,
    r033_deposit_unapplied,
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


def setup_books(conn: psycopg.Connection, client_id: UUID) -> dict[str, UUID]:
    customer = make_entity(conn, client_id, kind="customer", name="Acme Builders")
    return {
        "bank": make_account(conn, client_id, name="Checking", acct_type="Bank"),
        "cogs": make_account(conn, client_id, name="Job Materials",
                             acct_type="Cost of Goods Sold"),
        "overhead": make_account(conn, client_id, name="Office",
                                 acct_type="Expense"),
        "income": make_account(conn, client_id, name="Construction Income",
                               acct_type="Income"),
        "ar": make_account(conn, client_id, name="Accounts Receivable",
                           acct_type="Accounts Receivable"),
        "customer": customer,
        "job": make_job(conn, client_id, entity_id=customer,
                        name="Kitchen Remodel"),
    }


def test_r030_cogs_without_job(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)

    fires = balanced_purchase(conn, client_id, amount="400.00",
                              txn_date=date(2026, 6, 1),
                              bank=books["bank"], expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="200.00",  # non-fire: under floor
                      txn_date=date(2026, 6, 2),
                      bank=books["bank"], expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="400.00",  # non-fire: tagged
                      txn_date=date(2026, 6, 3), job=books["job"],
                      bank=books["bank"], expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="400.00",  # non-fire: not COGS
                      txn_date=date(2026, 6, 4),
                      bank=books["bank"], expense=books["overhead"])
    conn.commit()

    findings = r030_cogs_without_job.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["untagged_cogs"] == "400.00"


def test_r031_job_cost_after_completion(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    done = make_job(conn, client_id, entity_id=books["customer"],
                    name="Finished Deck", status="completed")
    closed = make_job(conn, client_id, entity_id=books["customer"],
                      name="Closed Garage", status="closed")

    fires_completed = balanced_purchase(
        conn, client_id, amount="300.00", txn_date=date(2026, 6, 1),
        job=done, bank=books["bank"], expense=books["cogs"])
    fires_closed = balanced_purchase(
        conn, client_id, amount="150.00", txn_date=date(2026, 6, 2),
        job=closed, bank=books["bank"], expense=books["cogs"])
    balanced_purchase(  # non-fire: active job
        conn, client_id, amount="300.00", txn_date=date(2026, 6, 3),
        job=books["job"], bank=books["bank"], expense=books["cogs"])
    conn.commit()

    findings = r031_job_cost_after_completion.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires_completed), str(fires_closed)}
    by_ref = {f.source_ref: f.detail for f in findings}
    assert by_ref[str(fires_completed)]["job_status"] == "completed"


def bill_job(
    conn: psycopg.Connection, client_id: UUID, books: dict[str, UUID],
    job: UUID, amount: str,
) -> None:
    make_txn(conn, client_id, txn_type="Invoice", txn_date=date(2026, 5, 1),
             entity_id=books["customer"], amount=amount,
             lines=[{"account": books["ar"], "amount": amount,
                     "posting": "debit", "job": job},
                    {"account": books["income"], "amount": amount,
                     "posting": "credit", "job": job}])


def cost_job(
    conn: psycopg.Connection, client_id: UUID, books: dict[str, UUID],
    job: UUID, amount: str,
) -> None:
    balanced_purchase(conn, client_id, amount=amount, txn_date=date(2026, 5, 10),
                      job=job, bank=books["bank"], expense=books["cogs"])


def test_r032_job_margin_negative(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    underwater = make_job(conn, client_id, entity_id=books["customer"],
                          name="Underwater Job")
    healthy = make_job(conn, client_id, entity_id=books["customer"],
                       name="Healthy Job")
    unbilled = make_job(conn, client_id, entity_id=books["customer"],
                        name="Unbilled Job")

    bill_job(conn, client_id, books, underwater, "500.00")
    cost_job(conn, client_id, books, underwater, "2000.00")  # margin -1500
    bill_job(conn, client_id, books, healthy, "3000.00")
    cost_job(conn, client_id, books, healthy, "2000.00")  # margin +1000
    cost_job(conn, client_id, books, unbilled, "800.00")  # no billing yet
    conn.commit()

    findings = r032_job_margin_negative.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(underwater)}
    assert findings[0].detail["margin"] == "-1500.00"
    assert r032_job_margin_negative.severity == "critical"


def test_r033_deposit_unapplied(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)

    def payment(days_old: int, linked: bool) -> UUID:
        when = AS_OF - timedelta(days=days_old)
        return make_txn(
            conn, client_id, txn_type="Payment", txn_date=when,
            entity_id=books["customer"], amount="1000.00",
            has_linked_txn=linked,
            lines=[{"account": books["bank"], "amount": "1000.00",
                    "posting": "debit"},
                   {"account": books["ar"], "amount": "1000.00",
                    "posting": "credit"}],
        )

    fires = payment(days_old=35, linked=False)
    payment(days_old=10, linked=False)  # non-fire: recent
    payment(days_old=35, linked=True)  # non-fire: applied
    conn.commit()

    findings = r033_deposit_unapplied.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["customer"] == "Acme Builders"
