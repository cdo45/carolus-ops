"""Unit + integration tests for the canonical → KPI-engine GL feed.

The pure helpers run with no database. The integration tests seed canonical
rows on the scratch Postgres (the shared ``conn`` fixture; skipped unless
CAROLUS_TEST_DB is set) and build a real engine SQLite DB under ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import date
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from analysis import engine_feed
from analysis.reconcile import engine_trial_balance
from tests.conftest import make_client
from tests.factories import make_account, make_entity, make_txn

# --------------------------------------------------------------------------- #
# Pure helpers — no database                                                   #
# --------------------------------------------------------------------------- #


def test_signed_amount_debit_is_positive() -> None:
    assert engine_feed.signed_amount("100.00", "debit") == 100.00
    assert engine_feed.signed_amount("100.00", "Debit") == 100.00


def test_signed_amount_credit_is_negative() -> None:
    assert engine_feed.signed_amount("100.00", "credit") == -100.00
    assert engine_feed.signed_amount("0", "credit") == 0.0


def test_signed_amount_rounds_to_cents() -> None:
    assert engine_feed.signed_amount("33.335", "debit") == 33.34


def test_signed_amount_rejects_unknown_posting() -> None:
    with pytest.raises(ValueError):
        engine_feed.signed_amount("1", "memo")


def test_gl_balances_value_shape() -> None:
    value = json.loads(engine_feed.gl_balances_value(1234.5))
    assert value["beginning_balance"] == 1234.5
    assert value["beginning_balance_source"] == engine_feed.BEGINNING_BALANCE_SOURCE
    assert value["upload_id"] is None
    assert value["declared_net_activity"] is None


def test_resolve_qbo_names_unique_passthrough() -> None:
    rows = [
        {"id": "a", "name": "Checking", "qbo_id": "1", "fqn": "Checking"},
        {"id": "b", "name": "Savings", "qbo_id": "2", "fqn": "Savings"},
    ]
    assert engine_feed.resolve_qbo_names(rows) == {"a": "Checking", "b": "Savings"}


def test_resolve_qbo_names_collision_falls_back_to_fqn() -> None:
    rows = [
        {"id": "a", "name": "Fees", "qbo_id": "1", "fqn": "Bank A:Fees"},
        {"id": "b", "name": "Fees", "qbo_id": "2", "fqn": "Bank B:Fees"},
    ]
    out = engine_feed.resolve_qbo_names(rows)
    assert out["a"] == "Fees"  # first holder keeps the bare leaf
    assert out["b"] == "Bank B:Fees"  # collision disambiguated by full path


def test_resolve_qbo_names_collision_without_fqn_uses_qbo_id() -> None:
    rows = [
        {"id": "a", "name": "Fees", "qbo_id": "1", "fqn": None},
        {"id": "b", "name": "Fees", "qbo_id": "2", "fqn": None},
    ]
    out = engine_feed.resolve_qbo_names(rows)
    assert out["a"] == "Fees"
    assert out["b"] == "Fees (2)"


# --------------------------------------------------------------------------- #
# Integration — scratch Postgres + a real engine SQLite DB                     #
# --------------------------------------------------------------------------- #


def _stage_account_payload(
    conn: psycopg.Connection, client_id: UUID, account_id: UUID, **fields: object
) -> None:
    """Stage a QBO ``Account`` payload in qbo_raw for an existing account, so
    the feed can read AcctNum / FullyQualifiedName from it."""
    qbo_id = conn.execute(
        "SELECT qbo_id FROM accounts WHERE id = %s", (account_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO qbo_raw (client_id, entity_type, qbo_id, payload) "
        "VALUES (%s, 'Account', %s, %s)",
        (client_id, qbo_id, Jsonb({"Id": qbo_id, **fields})),
    )


def _seed_mini_company(conn: psycopg.Connection) -> tuple[UUID, dict[str, UUID]]:
    """Two-account company with one prior-period and one in-period entry."""
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank",
                        acct_subtype="Checking")
    income = make_account(conn, client_id, name="Construction Income",
                          acct_type="Income")
    cust = make_entity(conn, client_id, kind="customer", name="Acme Builders")
    # Prior period: opening cash funded against income (kept simple).
    make_txn(conn, client_id, txn_type="Deposit", txn_date=date(2026, 4, 20),
             entity_id=cust, lines=[
                 {"account": bank, "amount": "1000.00", "posting": "debit"},
                 {"account": income, "amount": "1000.00", "posting": "credit"}])
    # In period: a sale settled to the bank.
    make_txn(conn, client_id, txn_type="SalesReceipt", txn_date=date(2026, 5, 10),
             entity_id=cust, doc_number="SR-1", lines=[
                 {"account": bank, "amount": "500.00", "posting": "debit"},
                 {"account": income, "amount": "500.00", "posting": "credit"}])
    _stage_account_payload(conn, client_id, bank, Name="Checking",
                           AcctNum="1000", FullyQualifiedName="Checking",
                           AccountType="Bank", AccountSubType="Checking")
    conn.commit()
    return client_id, {"bank": bank, "income": income}


def test_feed_populates_accounts_and_classifies(
    conn: psycopg.Connection, tmp_path
) -> None:
    client_id, _ = _seed_mini_company(conn)
    engine_conn, global_conn, report = engine_feed.build_engine_db(
        conn, client_id, "2026-05-01", "2026-05-31", tmp_path, now="2026-06-01T00:00:00+00:00"
    )
    try:
        assert report.accounts == 2
        rows = {r["qbo_name"]: r for r in engine_conn.execute(
            "SELECT qbo_name, qbo_type, detail_type, account_number, category, "
            "confidence, coa_balance FROM accounts")}
        # AcctNum sourced from qbo_raw; classifier filled category/confidence.
        assert rows["Checking"]["account_number"] == "1000"
        assert rows["Checking"]["qbo_type"] == "Bank"
        assert rows["Checking"]["category"] == "CASH"
        assert rows["Construction Income"]["category"] == "REV"
        assert rows["Checking"]["confidence"] >= 90
        # coa_balance carries the closing balance (1000 + 500).
        assert rows["Checking"]["coa_balance"] == 1500.0
    finally:
        engine_conn.close()
        global_conn.close()


def test_feed_signs_and_opening_balances(
    conn: psycopg.Connection, tmp_path
) -> None:
    client_id, accts = _seed_mini_company(conn)
    engine_conn, global_conn, report = engine_feed.build_engine_db(
        conn, client_id, "2026-05-01", "2026-05-31", tmp_path
    )
    try:
        # Period transactions: bank debit +500, income credit -500.
        amounts = {r["num"]: r["amount"] for r in engine_conn.execute(
            "SELECT t.num, t.amount FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id WHERE a.qbo_name='Checking'")}
        assert amounts["SR-1"] == 500.0
        income_amt = engine_conn.execute(
            "SELECT t.amount FROM transactions t JOIN accounts a "
            "ON a.id = t.account_id WHERE a.qbo_name='Construction Income'"
        ).fetchone()[0]
        assert income_amt == -500.0
        # Only in-period lines are loaded as transactions (2 lines in May).
        assert report.transactions == 2
        # One opening-balance audit row per account; bank opened at +1000.
        assert report.opening_rows == 2
        beginnings = engine_feed._engine().base.load_beginning_balances(engine_conn)
        bank_engine_id = report.account_id_map[str(accts["bank"])]
        assert beginnings[bank_engine_id] == 1000.0
    finally:
        engine_conn.close()
        global_conn.close()


def test_feed_idempotent_zero_drift(conn: psycopg.Connection, tmp_path) -> None:
    """Re-running the feed on the same engine DB produces zero duplicates and an
    identical trial balance (Principle: idempotency on every write)."""
    client_id, _ = _seed_mini_company(conn)
    engine_conn, global_conn, _ = engine_feed.build_engine_db(
        conn, client_id, "2026-05-01", "2026-05-31", tmp_path
    )
    try:
        first = engine_trial_balance(engine_conn, "2026-05-31")
        n_acct = engine_conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        n_txn = engine_conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
        # Re-feed the same open connection.
        engine_feed.feed_engine(
            conn, engine_conn, global_conn, client_id, "2026-05-01", "2026-05-31"
        )
        assert engine_conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == n_acct
        assert engine_conn.execute(
            "SELECT count(*) FROM transactions").fetchone()[0] == n_txn
        assert engine_trial_balance(engine_conn, "2026-05-31") == first
    finally:
        engine_conn.close()
        global_conn.close()
