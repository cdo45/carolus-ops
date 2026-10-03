"""Trial-balance reconciliation — the gate that proves the sign mapping.

Before any KPI is trusted, the engine's computed trial balance (opening balance
+ activity, per the debit→+/credit→− mapping in :mod:`analysis.engine_feed`)
must tie out to a known-good QBO Trial Balance for the same client/period. A
flipped debit/credit sign would put asset balances in the credit column and the
reconciliation would fail — which is exactly what this gate is here to catch.

The engine trial balance is computed the engine's own way: per account,
``beginning_balance`` (read back from the ``gl_balances`` audit rows via
``kpi_engine/core/kpi/base.py``) plus the signed activity through the as-of
date — across ALL accounts, with no category filter or dormancy exclusion, so
it matches a full QBO TB. Signed balances render as a QBO-style debit/credit
TB: a positive (debit) balance lands in the Debit column, a negative one in
Credit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from analysis.engine_feed import _engine


@dataclass(frozen=True)
class TBRow:
    """One trial-balance line in QBO's debit/credit shape."""

    qbo_name: str
    account_number: str | None
    balance: float  # signed: debit-positive
    debit: float
    credit: float


@dataclass
class ReconcileResult:
    """Per-account comparison of engine vs QBO balances."""

    ties_out: bool
    total_debits: float
    total_credits: float
    out_of_balance: float  # engine: sum of signed balances; ~0 for double entry
    diffs: list[dict[str, object]] = field(default_factory=list)
    only_in_engine: list[str] = field(default_factory=list)
    only_in_qbo: list[str] = field(default_factory=list)


def signed_to_debit_credit(balance: float) -> tuple[float, float]:
    """Signed balance → (debit, credit) columns, QBO convention."""
    b = round(float(balance), 2)
    return (b, 0.0) if b >= 0 else (0.0, -b)


def engine_trial_balance(
    engine_conn: sqlite3.Connection, as_of_date: str
) -> dict[str, float]:
    """Signed balance per ``qbo_name`` as of ``as_of_date``, the engine's way.

    balance = beginning_balance (from the gl_balances audit rows) + sum of
    signed transaction amounts with ``txn_date <= as_of_date``. Covers every
    account, including those that net to zero.
    """
    engine = _engine()
    beginnings = engine.base.load_beginning_balances(engine_conn)
    rows = engine_conn.execute(
        """
        SELECT a.id, a.qbo_name,
               COALESCE((SELECT SUM(t.amount) FROM transactions t
                         WHERE t.account_id = a.id AND t.txn_date <= ?), 0)
                   AS activity
        FROM accounts a
        """,
        (as_of_date,),
    ).fetchall()
    return {
        r["qbo_name"]: round(beginnings.get(r["id"], 0.0) + r["activity"], 2)
        for r in rows
    }


def engine_trial_balance_rows(
    engine_conn: sqlite3.Connection, as_of_date: str
) -> list[TBRow]:
    """:func:`engine_trial_balance` as sorted, debit/credit-split TB rows."""
    numbers = {
        r["qbo_name"]: r["account_number"]
        for r in engine_conn.execute(
            "SELECT qbo_name, account_number FROM accounts"
        )
    }
    out: list[TBRow] = []
    for name, balance in engine_trial_balance(engine_conn, as_of_date).items():
        debit, credit = signed_to_debit_credit(balance)
        out.append(TBRow(name, numbers.get(name), balance, debit, credit))
    out.sort(key=lambda r: (r.account_number or "~", r.qbo_name))
    return out


def reconcile(
    engine_tb: dict[str, float],
    qbo_tb: dict[str, float],
    *,
    tol: float = 0.005,
) -> ReconcileResult:
    """Compare engine vs known-good QBO signed balances per account.

    Both maps are keyed by account name → signed balance (debit-positive).
    Ties out when every shared account agrees within ``tol``, neither side has
    an account the other lacks (beyond a zero balance), and the engine TB is
    itself balanced (signed total ≈ 0).
    """
    names = set(engine_tb) | set(qbo_tb)
    diffs: list[dict[str, object]] = []
    only_in_engine: list[str] = []
    only_in_qbo: list[str] = []

    for name in sorted(names):
        e = round(engine_tb.get(name, 0.0), 2)
        q = round(qbo_tb.get(name, 0.0), 2)
        if name not in qbo_tb and abs(e) > tol:
            only_in_engine.append(name)
        if name not in engine_tb and abs(q) > tol:
            only_in_qbo.append(name)
        if abs(e - q) > tol:
            diffs.append({"account": name, "engine": e, "qbo": q,
                          "delta": round(e - q, 2)})

    total_debits = round(sum(v for v in engine_tb.values() if v > 0), 2)
    total_credits = round(-sum(v for v in engine_tb.values() if v < 0), 2)
    out_of_balance = round(sum(engine_tb.values()), 2)

    ties_out = (
        not diffs
        and not only_in_engine
        and not only_in_qbo
        and abs(out_of_balance) <= tol
    )
    return ReconcileResult(
        ties_out=ties_out,
        total_debits=total_debits,
        total_credits=total_credits,
        out_of_balance=out_of_balance,
        diffs=diffs,
        only_in_engine=only_in_engine,
        only_in_qbo=only_in_qbo,
    )
