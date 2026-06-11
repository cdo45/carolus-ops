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

    fires = balanced_purchase(conn, client_id, amount="600.00",
                              txn_date=date(2026, 6, 1),
                              bank=books["bank"], expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="400.00",  # non-fire: under the
                      txn_date=date(2026, 6, 2),  # recalibrated $500 floor
                      bank=books["bank"], expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="600.00",  # non-fire: tagged
                      txn_date=date(2026, 6, 3), job=books["job"],
                      bank=books["bank"], expense=books["cogs"])
    balanced_purchase(conn, client_id, amount="600.00",  # non-fire: not COGS
                      txn_date=date(2026, 6, 4),
                      bank=books["bank"], expense=books["overhead"])
    conn.commit()

    findings = r030_cogs_without_job.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["untagged_cogs"] == "600.00"


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


def test_r031_completed_at_grants_grace_window(
    conn: psycopg.Connection,
) -> None:
    """Punch-list costs within 14 days of completed_at are close-out, not
    margin rewriting; day 15+ fires."""
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    done = make_job(conn, client_id, entity_id=books["customer"],
                    name="Dated Deck", status="completed",
                    completed_at=date(2026, 5, 1))

    balanced_purchase(  # non-fire: 9 days after completion (inside grace)
        conn, client_id, amount="300.00", txn_date=date(2026, 5, 10),
        job=done, bank=books["bank"], expense=books["cogs"])
    fires = balanced_purchase(  # 19 days after completion: outside grace
        conn, client_id, amount="450.00", txn_date=date(2026, 5, 20),
        job=done, bank=books["bank"], expense=books["cogs"])
    conn.commit()

    findings = r031_job_cost_after_completion.run(conn, client_id, AS_OF)
    assert refs(findings) == {str(fires)}


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
    job: UUID, amount: str, when: date = date(2026, 5, 10),
) -> None:
    balanced_purchase(conn, client_id, amount=amount, txn_date=when,
                      job=job, bank=books["bank"], expense=books["cogs"])


def test_r032_severity_splits_on_depth_and_maturity(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    deep_mature = make_job(conn, client_id, entity_id=books["customer"],
                           name="Deep Mature Job")
    deep_recent = make_job(conn, client_id, entity_id=books["customer"],
                           name="Deep Recent Job")
    slight = make_job(conn, client_id, entity_id=books["customer"],
                      name="Slightly Under Job")
    healthy = make_job(conn, client_id, entity_id=books["customer"],
                       name="Healthy Job")
    unbilled = make_job(conn, client_id, entity_id=books["customer"],
                        name="Unbilled Job")

    # critical: 400% of billings AND first cost 51 days old
    bill_job(conn, client_id, books, deep_mature, "500.00")
    cost_job(conn, client_id, books, deep_mature, "2000.00",
             when=date(2026, 4, 20))
    # info: just as deep but the first cost is only 26 days old
    bill_job(conn, client_id, books, deep_recent, "500.00")
    cost_job(conn, client_id, books, deep_recent, "2000.00",
             when=date(2026, 5, 15))
    # info: mature but only 105% of billings (under the 110% bar)
    bill_job(conn, client_id, books, slight, "1000.00")
    cost_job(conn, client_id, books, slight, "1050.00",
             when=date(2026, 4, 20))
    bill_job(conn, client_id, books, healthy, "3000.00")
    cost_job(conn, client_id, books, healthy, "2000.00")  # margin +1000
    cost_job(conn, client_id, books, unbilled, "800.00")  # no billing yet
    conn.commit()

    findings = r032_job_margin_negative.run(conn, client_id, AS_OF)

    grades = {f.source_ref: (f.severity, f.detail["grade"])
              for f in findings}
    assert grades == {
        str(deep_mature): ("critical", "critical"),
        str(deep_recent): ("info", "info"),
        str(slight): ("info", "info"),
    }
    by_ref = {f.source_ref: f.detail for f in findings}
    assert by_ref[str(deep_mature)]["margin"] == "-1500.00"


def test_r033_deposit_unapplied(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)

    def payment(days_old: int, linked: bool, amount: str = "1000.00") -> UUID:
        when = AS_OF - timedelta(days=days_old)
        return make_txn(
            conn, client_id, txn_type="Payment", txn_date=when,
            entity_id=books["customer"], amount=amount,
            has_linked_txn=linked,
            lines=[{"account": books["bank"], "amount": amount,
                    "posting": "debit"},
                   {"account": books["ar"], "amount": amount,
                    "posting": "credit"}],
        )

    fires = payment(days_old=35, linked=False)
    payment(days_old=10, linked=False)  # non-fire: recent
    payment(days_old=35, linked=True)  # non-fire: applied
    payment(days_old=35, linked=False, amount="300.00")  # non-fire: < $500
    conn.commit()

    findings = r033_deposit_unapplied.run(conn, client_id, AS_OF)

    assert refs(findings) == {str(fires)}
    assert findings[0].detail["customer"] == "Acme Builders"
