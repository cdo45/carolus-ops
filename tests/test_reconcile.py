"""Trial-balance reconciliation — unit checks plus the Part-1 gate.

The gate seeds a small but complete double-entry construction company on the
scratch Postgres, feeds the KPI engine through the adapter, then reconciles the
engine's computed trial balance against a hand-authored known-good QBO Trial
Balance. It ties out only if debit/credit → sign maps correctly and opening
balance + period activity recombine to the right closing balances.

Prior-period activity touches balance-sheet accounts only (opened via Opening
Balance Equity), so the engine's cumulative balance as of period end equals
what a QBO Trial Balance for the period reports: ending balances for BS
accounts, period activity for P&L accounts.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from analysis import engine_feed
from analysis.reconcile import (
    engine_trial_balance,
    engine_trial_balance_rows,
    reconcile,
    signed_to_debit_credit,
)
from tests.conftest import make_client
from tests.factories import make_account, make_entity, make_txn

PERIOD_START = "2026-05-01"
PERIOD_END = "2026-05-31"

# Known-good QBO Trial Balance as of 2026-05-31 (signed, debit-positive).
# Authored independently of the journal entries below; the gate proves the
# engine reproduces these from canonical GL.
QBO_TRIAL_BALANCE: dict[str, float] = {
    "Checking": 12_700.00,
    "Accounts Receivable": 3_000.00,
    "Accounts Payable": -1_700.00,
    "Construction Income": -5_000.00,
    "Job Materials": 1_200.00,
    "Office Expenses": 300.00,
    "Opening Balance Equity": -10_500.00,
}


# --------------------------------------------------------------------------- #
# Pure unit tests                                                              #
# --------------------------------------------------------------------------- #


def test_signed_to_debit_credit() -> None:
    assert signed_to_debit_credit(100.0) == (100.0, 0.0)
    assert signed_to_debit_credit(-100.0) == (0.0, 100.0)
    assert signed_to_debit_credit(0.0) == (0.0, 0.0)


def test_reconcile_ties_out_when_equal() -> None:
    tb = {"Cash": 100.0, "Income": -100.0}
    result = reconcile(tb, dict(tb))
    assert result.ties_out
    assert result.out_of_balance == 0.0
    assert result.total_debits == 100.0
    assert result.total_credits == 100.0
    assert result.diffs == []


def test_reconcile_flags_per_account_difference() -> None:
    engine = {"Cash": 90.0, "Income": -100.0}  # Cash 10 short, also unbalanced
    qbo = {"Cash": 100.0, "Income": -100.0}
    result = reconcile(engine, qbo)
    assert not result.ties_out
    assert result.diffs == [{"account": "Cash", "engine": 90.0, "qbo": 100.0,
                             "delta": -10.0}]
    assert result.out_of_balance == -10.0


def test_reconcile_flags_missing_accounts() -> None:
    result = reconcile({"Cash": 100.0, "Ghost": 5.0}, {"Cash": 100.0})
    assert not result.ties_out
    assert result.only_in_engine == ["Ghost"]


# --------------------------------------------------------------------------- #
# The gate — full feed end-to-end on the scratch DB                            #
# --------------------------------------------------------------------------- #


def _seed_construction_company(conn: psycopg.Connection) -> UUID:
    """A balanced GL whose closing balances equal QBO_TRIAL_BALANCE."""
    client_id = make_client(conn)
    a = {
        "Checking": make_account(conn, client_id, name="Checking",
                                 acct_type="Bank", acct_subtype="Checking"),
        "Accounts Receivable": make_account(conn, client_id,
                                            name="Accounts Receivable",
                                            acct_type="Accounts Receivable"),
        "Accounts Payable": make_account(conn, client_id,
                                         name="Accounts Payable",
                                         acct_type="Accounts Payable"),
        "Construction Income": make_account(conn, client_id,
                                            name="Construction Income",
                                            acct_type="Income"),
        "Job Materials": make_account(conn, client_id, name="Job Materials",
                                      acct_type="Cost of Goods Sold"),
        "Office Expenses": make_account(conn, client_id, name="Office Expenses",
                                        acct_type="Expense"),
        "Opening Balance Equity": make_account(conn, client_id,
                                               name="Opening Balance Equity",
                                               acct_type="Equity"),
    }
    cust = make_entity(conn, client_id, kind="customer", name="Acme Builders")
    vend = make_entity(conn, client_id, kind="vendor", name="Home Depot")

    def je(txn_type: str, when: date, lines: list[dict[str, object]], **kw: object) -> None:
        make_txn(conn, client_id, txn_type=txn_type, txn_date=when, lines=lines, **kw)

    # --- opening balances (before the period): BS accounts only -------------
    opening = date(2026, 4, 15)
    je("JournalEntry", opening, [
        {"account": a["Checking"], "amount": "10000.00", "posting": "debit"},
        {"account": a["Opening Balance Equity"], "amount": "10000.00",
         "posting": "credit"}])
    je("JournalEntry", opening, [
        {"account": a["Accounts Receivable"], "amount": "2000.00", "posting": "debit"},
        {"account": a["Opening Balance Equity"], "amount": "2000.00",
         "posting": "credit"}])
    je("JournalEntry", opening, [
        {"account": a["Opening Balance Equity"], "amount": "1500.00", "posting": "debit"},
        {"account": a["Accounts Payable"], "amount": "1500.00", "posting": "credit"}])

    # --- in-period activity --------------------------------------------------
    je("Invoice", date(2026, 5, 3), entity_id=cust, doc_number="INV-1001", lines=[
        {"account": a["Accounts Receivable"], "amount": "5000.00", "posting": "debit"},
        {"account": a["Construction Income"], "amount": "5000.00", "posting": "credit"}])
    je("Payment", date(2026, 5, 10), entity_id=cust, doc_number="PMT-1", lines=[
        {"account": a["Checking"], "amount": "4000.00", "posting": "debit"},
        {"account": a["Accounts Receivable"], "amount": "4000.00", "posting": "credit"}])
    je("Bill", date(2026, 5, 5), entity_id=vend, doc_number="BILL-7", lines=[
        {"account": a["Job Materials"], "amount": "1200.00", "posting": "debit"},
        {"account": a["Accounts Payable"], "amount": "1200.00", "posting": "credit"}])
    je("BillPayment", date(2026, 5, 20), entity_id=vend, doc_number="BP-9", lines=[
        {"account": a["Accounts Payable"], "amount": "1000.00", "posting": "debit"},
        {"account": a["Checking"], "amount": "1000.00", "posting": "credit"}])
    je("Purchase", date(2026, 5, 15), entity_id=vend, doc_number="EXP-3", lines=[
        {"account": a["Office Expenses"], "amount": "300.00", "posting": "debit"},
        {"account": a["Checking"], "amount": "300.00", "posting": "credit"}])
    conn.commit()
    return client_id


def test_reconciliation_gate(conn: psycopg.Connection, tmp_path) -> None:
    client_id = _seed_construction_company(conn)
    engine_conn, global_conn, report = engine_feed.build_engine_db(
        conn, client_id, PERIOD_START, PERIOD_END, tmp_path
    )
    try:
        assert report.skipped_undated_lines == 0  # nothing silently dropped
        engine_tb = engine_trial_balance(engine_conn, PERIOD_END)
        result = reconcile(engine_tb, QBO_TRIAL_BALANCE)

        # The gate: the engine's TB ties out to the known-good QBO TB.
        assert result.ties_out, (
            f"trial balance did not reconcile: diffs={result.diffs} "
            f"only_in_engine={result.only_in_engine} "
            f"only_in_qbo={result.only_in_qbo} "
            f"out_of_balance={result.out_of_balance}"
        )
        # Double entry: TB balances to zero, debits == credits.
        assert result.out_of_balance == 0.0
        assert result.total_debits == result.total_credits == 17_200.00

        # Sign sanity, stated directly: a Bank account with net debits is a
        # positive (debit) balance — the assertion a flipped mapping fails.
        assert engine_tb["Checking"] > 0
        assert engine_tb["Construction Income"] < 0

        # The rendered TB is QBO-shaped (debit/credit columns) and complete.
        rows = engine_trial_balance_rows(engine_conn, PERIOD_END)
        assert {r.qbo_name for r in rows} == set(QBO_TRIAL_BALANCE)
        checking = next(r for r in rows if r.qbo_name == "Checking")
        assert (checking.debit, checking.credit) == (12_700.00, 0.0)
    finally:
        engine_conn.close()
        global_conn.close()


def test_reconciliation_gate_idempotent(conn: psycopg.Connection, tmp_path) -> None:
    """Building the engine DB twice (fresh dirs) yields the identical TB."""
    client_id = _seed_construction_company(conn)
    first_conn, first_global, _ = engine_feed.build_engine_db(
        conn, client_id, PERIOD_START, PERIOD_END, tmp_path / "run1"
    )
    second_conn, second_global, _ = engine_feed.build_engine_db(
        conn, client_id, PERIOD_START, PERIOD_END, tmp_path / "run2"
    )
    try:
        assert (engine_trial_balance(first_conn, PERIOD_END)
                == engine_trial_balance(second_conn, PERIOD_END)
                == QBO_TRIAL_BALANCE)
    finally:
        first_conn.close()
        first_global.close()
        second_conn.close()
        second_global.close()
