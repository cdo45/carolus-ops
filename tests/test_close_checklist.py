"""Close checklist: condition evaluation, overall status, zero-drift storage."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.close_checklist import (
    evaluate_close,
    parse_period,
    persist_close,
)
from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account

PERIOD_START, PERIOD_END = date(2026, 6, 1), date(2026, 6, 30)


def setup_books(conn: psycopg.Connection, client_id: UUID) -> dict[str, UUID]:
    return {
        "bank": make_account(conn, client_id, name="Checking", acct_type="Bank"),
        "expense": make_account(conn, client_id, name="Office",
                                acct_type="Expense"),
    }


def states(result: object) -> dict[str, str]:
    return {c.name: c.state for c in result.conditions}  # type: ignore[attr-defined]


def test_parse_period() -> None:
    assert parse_period("2026-06") == (date(2026, 6, 1), date(2026, 6, 30))
    assert parse_period("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert parse_period("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))


def test_healthy_books_are_incomplete_until_phase4(
    conn: psycopg.Connection,
) -> None:
    """Doc condition is a stub — it must hold the close at incomplete,
    never silently pass."""
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    balanced_purchase(conn, client_id, amount="100.00",
                      txn_date=date(2026, 6, 5), **books)
    conn.commit()

    result = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)

    assert states(result) == {
        "no_open_critical_flags": "pass",
        "suspense_zeroed": "pass",
        "no_stale_uncategorized": "pass",
        "documents_reviewed": "not_evaluated",
    }
    assert result.status == "incomplete"
    doc = next(c for c in result.conditions if c.name == "documents_reviewed")
    assert doc.detail["doc_status_counts"] == {"unbacked": 1}


def test_open_critical_flag_turns_red(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status, source_type,
                           source_ref, detail)
        VALUES (%s, 'R010', 'critical', 'open', 'transaction', 'ref-x', '{}')
        """,
        (client_id,),
    )
    conn.commit()

    result = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)

    assert result.status == "red"
    assert states(result)["no_open_critical_flags"] == "fail"
    critical = next(
        c for c in result.conditions if c.name == "no_open_critical_flags"
    )
    assert critical.detail["open_critical_by_rule"] == {"R010": 1}


def test_suspense_balance_turns_red(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    suspense = make_account(conn, client_id, name="Ask My Accountant",
                            acct_type="Other Expense")
    balanced_purchase(conn, client_id, amount="123.00",
                      txn_date=date(2026, 6, 20),  # recent counts too at close
                      bank=books["bank"], expense=suspense)
    conn.commit()

    result = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)

    assert result.status == "red"
    suspense_cond = next(
        c for c in result.conditions if c.name == "suspense_zeroed"
    )
    assert suspense_cond.state == "fail"
    assert suspense_cond.detail["nonzero_balances"] == {
        "Ask My Accountant": "123.00"
    }


def test_stale_uncategorized_turns_red(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = setup_books(conn, client_id)
    uncategorized = make_account(conn, client_id, name="Uncategorized Expense",
                                 acct_type="Expense")
    balanced_purchase(conn, client_id, amount="55.00",
                      txn_date=date(2026, 6, 1),  # 29 days before period end
                      bank=books["bank"], expense=uncategorized)
    conn.commit()

    result = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)

    assert result.status == "red"
    assert states(result)["no_stale_uncategorized"] == "fail"


def test_persistence_is_zero_drift(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    setup_books(conn, client_id)
    conn.commit()

    result = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)
    assert persist_close(conn, client_id, result) == 1, "first write inserts"

    stamp = conn.execute(
        "SELECT evaluated_at, status FROM close_runs WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    assert stamp is not None

    again = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)
    assert persist_close(conn, client_id, again) == 0, "identical re-eval: no write"
    assert conn.execute(
        "SELECT evaluated_at, status FROM close_runs WHERE client_id = %s",
        (client_id,),
    ).fetchone() == stamp, "row untouched, evaluated_at preserved"

    conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status, source_type,
                           source_ref, detail)
        VALUES (%s, 'R011', 'critical', 'open', 'transaction', 'ref-y', '{}')
        """,
        (client_id,),
    )
    conn.commit()
    changed = evaluate_close(conn, client_id, PERIOD_START, PERIOD_END)
    assert persist_close(conn, client_id, changed) == 1, "changed books: one write"
    status = conn.execute(
        "SELECT status FROM close_runs WHERE client_id = %s", (client_id,)
    ).fetchone()
    assert status == ("red",)
    count = conn.execute(
        "SELECT count(*) FROM close_runs WHERE client_id = %s", (client_id,)
    ).fetchone()
    assert count == (1,), "still one row per (client, period)"
