"""Bank rec verifier: exact matching, fuzzy annotation, R040/R041 flags,
tie determination, idempotent re-runs, and the close-checklist feed."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from docpipe.bankrec import rec
from rules.close_checklist import evaluate_close
from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account, make_txn

PERIOD_START, PERIOD_END = "2026-05-01", "2026-05-31"


def statement_doc_row(
    conn: psycopg.Connection, client_id: UUID, lines: list[dict[str, Any]],
    *, beginning: str = "5000.00", ending: str, sha_suffix: str = "01",
) -> UUID:
    extracted = {
        "period_start": PERIOD_START, "period_end": PERIOD_END,
        "beginning_balance": beginning, "ending_balance": ending,
        "stated_txn_count": len(lines), "lines": lines,
    }
    row = conn.execute(
        """
        INSERT INTO documents (client_id, storage_ref, sha256, status,
                               doc_type, extracted, period_start, period_end)
        VALUES (%s, %s, %s, 'validated', 'bank_statement', %s, %s, %s)
        RETURNING id
        """,
        (client_id, "k" + sha_suffix, "sha-" + sha_suffix, Jsonb(extracted),
         PERIOD_START, PERIOD_END),
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def line(day: str, description: str, amount: str, direction: str) -> dict[str, Any]:
    return {"date": f"2026-05-{day}", "description": description,
            "amount": amount, "direction": direction}


def seed_books(conn: psycopg.Connection, client_id: UUID) -> dict[str, UUID]:
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")
    # money out: purchases credit the bank account
    balanced_purchase(conn, client_id, amount="750.00",
                      txn_date=date(2026, 5, 3), bank=bank, expense=expense)
    balanced_purchase(conn, client_id, amount="89.99",
                      txn_date=date(2026, 5, 12), bank=bank, expense=expense)
    # money in: a deposit debits the bank account
    make_txn(conn, client_id, txn_type="Deposit", txn_date=date(2026, 5, 10),
             amount="1500.00",
             lines=[{"account": bank, "amount": "1500.00", "posting": "debit"},
                    {"account": expense, "amount": "1500.00",
                     "posting": "credit"}])
    # uncleared and aged: in the candidate window, 32 days old at period end
    balanced_purchase(conn, client_id, amount="200.00",
                      txn_date=date(2026, 4, 29), bank=bank, expense=expense)
    conn.commit()
    return {"bank": bank, "expense": expense}


CLEAN_LINES = [
    line("03", "CHECK 1402 ACME SUPPLY", "750.00", "debit"),
    line("10", "BRANCH DEPOSIT", "1500.00", "credit"),
    line("12", "CARD PURCHASE HOME DEPOT", "89.99", "debit"),
]
CLEAN_ENDING = "5660.01"  # 5000 + 1500 - 750 - 89.99


def open_flags(conn: psycopg.Connection, client_id: UUID, code: str) -> list[tuple]:
    return conn.execute(
        "SELECT source_type, source_ref, detail FROM flags"
        " WHERE client_id = %s AND rule_code = %s AND status = 'open'",
        (client_id, code),
    ).fetchall()


def test_clean_statement_ties(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    doc_id = statement_doc_row(conn, client_id, CLEAN_LINES, ending=CLEAN_ENDING)

    result = rec(conn, client_id, books["bank"], doc_id)

    assert result.tied is True
    assert result.matched_count == 3
    assert result.unmatched_statement_lines == []
    assert open_flags(conn, client_id, "R040") == []
    # the aged uncleared purchase is outstanding + R041, but doesn't break the tie
    assert [item["amount"] for item in result.outstanding_items] == ["200.00"]
    assert result.outstanding_items[0]["age_days"] == 32
    r041 = open_flags(conn, client_id, "R041")
    assert len(r041) == 1 and r041[0][0] == "transaction"
    row = conn.execute(
        "SELECT tied, matched_count, qbo_cleared_balance FROM rec_runs"
        " WHERE id = %s",
        (result.rec_run_id,),
    ).fetchone()
    assert row == (True, 3, Decimal("5660.01"))


def test_phantom_line_breaks_tie_and_raises_r040(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    phantom = line("20", "ATM WITHDRAWAL", "45.00", "debit")
    doc_id = statement_doc_row(
        conn, client_id, [*CLEAN_LINES, phantom], ending="5615.01",
    )

    result = rec(conn, client_id, books["bank"], doc_id)

    assert result.tied is False
    assert result.matched_count == 3
    assert len(result.unmatched_statement_lines) == 1
    assert result.unmatched_statement_lines[0]["description"] == "ATM WITHDRAWAL"
    flags = open_flags(conn, client_id, "R040")
    assert len(flags) == 1
    source_type, source_ref, detail = flags[0]
    assert source_type == "document" and source_ref == str(doc_id), (
        "R040 points at the statement document — a valid canonical row"
    )
    assert "ATM WITHDRAWAL" in detail


def test_fuzzy_candidate_annotates_but_never_matches(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    # books have it on 5/30; the statement shows 5/18 — 12 days apart
    balanced_purchase(conn, client_id, amount="64.50",
                      txn_date=date(2026, 5, 30),
                      bank=books["bank"], expense=books["expense"])
    conn.commit()
    shifted = line("18", "VENDOR DRAFT", "64.50", "debit")
    doc_id = statement_doc_row(
        conn, client_id, [*CLEAN_LINES, shifted], ending="5595.51",
    )

    result = rec(conn, client_id, books["bank"], doc_id)

    assert result.matched_count == 3, "fuzzy is flag-only, not a match"
    (unmatched,) = result.unmatched_statement_lines
    assert unmatched["fuzzy_candidate"]["days_apart"] == 12
    (r040,) = open_flags(conn, client_id, "R040")
    assert "fuzzy_candidate" in r040[2]


def test_rerun_is_idempotent(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    phantom = line("20", "ATM WITHDRAWAL", "45.00", "debit")
    doc_id = statement_doc_row(
        conn, client_id, [*CLEAN_LINES, phantom], ending="5615.01",
    )

    first = rec(conn, client_id, books["bank"], doc_id)
    snapshot = conn.execute(
        "SELECT id::text, xmin::text FROM rec_runs"
    ).fetchall()
    second = rec(conn, client_id, books["bank"], doc_id)

    assert second.rec_run_id == first.rec_run_id
    assert second.flags_created == 0, "no duplicate R040/R041"
    assert conn.execute(
        "SELECT id::text, xmin::text FROM rec_runs"
    ).fetchall() == snapshot, "unchanged re-rec writes zero rows"
    assert len(open_flags(conn, client_id, "R040")) == 1


def test_rec_requires_validated_statement(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    doc_id = statement_doc_row(conn, client_id, CLEAN_LINES, ending=CLEAN_ENDING)
    conn.execute("UPDATE documents SET status = 'escalated' WHERE id = %s",
                 (doc_id,))
    conn.commit()
    with pytest.raises(ValueError, match="VALIDATED bank_statement"):
        rec(conn, client_id, books["bank"], doc_id)


def test_tied_rec_feeds_close_checklist(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    doc_id = statement_doc_row(conn, client_id, CLEAN_LINES, ending=CLEAN_ENDING)
    rec(conn, client_id, books["bank"], doc_id)
    # resolve the R041 so no fail-states distract; close period = May
    conn.execute(
        "UPDATE flags SET status='resolved', resolution_note='checked',"
        " resolved_at=now() WHERE client_id=%s",
        (client_id,),
    )
    conn.commit()

    result = evaluate_close(conn, client_id, date(2026, 5, 1), date(2026, 5, 31))

    docs = next(c for c in result.conditions if c.name == "documents_reviewed")
    assert docs.state == "pass"
    assert docs.detail["bank_recs"] == {"Checking": "pass"}
    assert result.status == "green", "first fully-green close"


def test_untied_rec_fails_close(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    phantom = line("20", "ATM WITHDRAWAL", "45.00", "debit")
    doc_id = statement_doc_row(
        conn, client_id, [*CLEAN_LINES, phantom], ending="5615.01",
    )
    rec(conn, client_id, books["bank"], doc_id)

    result = evaluate_close(conn, client_id, date(2026, 5, 1), date(2026, 5, 31))

    docs = next(c for c in result.conditions if c.name == "documents_reviewed")
    assert docs.state == "fail"
    assert docs.detail["bank_recs"] == {"Checking": "fail"}
    assert result.status == "red"


def test_unreced_bank_account_stays_not_evaluated(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    books = seed_books(conn, client_id)
    make_account(conn, client_id, name="Savings", acct_type="Bank")
    doc_id = statement_doc_row(conn, client_id, CLEAN_LINES, ending=CLEAN_ENDING)
    rec(conn, client_id, books["bank"], doc_id)
    conn.commit()

    result = evaluate_close(conn, client_id, date(2026, 5, 1), date(2026, 5, 31))

    docs = next(c for c in result.conditions if c.name == "documents_reviewed")
    assert docs.state == "not_evaluated", "one tied account never greens the rest"
    assert docs.detail["bank_recs"] == {
        "Checking": "pass", "Savings": "not_evaluated",
    }
