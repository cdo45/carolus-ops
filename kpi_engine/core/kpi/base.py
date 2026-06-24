"""Shared KPI plumbing.

Every KPI computes from the client SQLite database: transactions joined to
accounts, filtered by accounts.category (taxonomy codes) — never by account
name. Two account groups get special treatment everywhere:

  * excluded — dormant accounts whose COA balance is zero or unknown
    (dormant = 1 AND coa_balance IN (0, NULL)); they contribute to nothing.
  * unmapped — category IS NULL; they contribute to no computed KPI but are
    COUNTED, lowering confidence via unmapped_share().

Balance-sheet balances as of a date D = beginning_balance + sum(amounts
where txn_date <= D). Beginning balances are read back from the audit rows
the GL importer wrote (entity 'accounts', field 'gl_balances'); the latest
row per account wins, and an account with no row begins at 0.0. P&L
categories are pure date-range sums — no beginning balance.

Sign conventions: asset/expense accounts carry natural positive balances;
liability/equity/income accounts carry whatever signs QBO exported. Balances
are always beginning + activity per account — never flipped by category.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from core.classify import BALANCE_SHEET_TYPES, EXPENSE_TYPES

PL_TYPES = EXPENSE_TYPES | {"income", "other income"}

# Exclusion rule shared by every KPI query; alias the accounts table `a`.
NOT_EXCLUDED = (
    "NOT (a.dormant = 1 AND (a.coa_balance IS NULL OR a.coa_balance = 0))"
)


@dataclass
class KPIValue:
    key: str
    label: str
    value: float | None
    unit: str  # "currency" | "ratio" | "weeks" | "percent" | "months"
    confidence: str  # "high" | "medium" | "low"
    detail: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def load_beginning_balances(conn: sqlite3.Connection) -> dict[int, float]:
    """Beginning balances persisted by the GL importer; latest row per
    account wins (rows are scanned in id order, later uploads overwrite)."""
    beginnings: dict[int, float] = {}
    rows = conn.execute(
        """
        SELECT entity_id, new_value FROM audit_log
        WHERE entity = 'accounts' AND field = 'gl_balances'
          AND source = 'import'
        ORDER BY id
        """
    )
    for r in rows:
        beginning = json.loads(r["new_value"]).get("beginning_balance")
        if beginning is not None:
            beginnings[r["entity_id"]] = float(beginning)
    return beginnings


def balance_as_of(
    conn: sqlite3.Connection,
    categories: list[str],
    date: str,
    beginnings: dict[int, float],
) -> tuple[float, dict[int, float]]:
    """Sum of (beginning + activity through `date`) across non-excluded
    accounts in `categories`; returns (total, {account_id: balance})."""
    qmarks = ",".join("?" * len(categories))
    rows = conn.execute(
        f"""
        SELECT a.id,
               COALESCE((SELECT SUM(t.amount) FROM transactions t
                         WHERE t.account_id = a.id AND t.txn_date <= ?), 0)
                   AS activity
        FROM accounts a
        WHERE a.category IN ({qmarks}) AND {NOT_EXCLUDED}
        """,
        [date, *categories],
    ).fetchall()
    per_account = {
        r["id"]: round(beginnings.get(r["id"], 0.0) + r["activity"], 2)
        for r in rows
    }
    return round(sum(per_account.values()), 2), per_account


def activity_sum(
    conn: sqlite3.Connection, categories: list[str], start: str, end: str
) -> float:
    """Signed sum of transaction amounts in `categories` over [start, end]."""
    qmarks = ",".join("?" * len(categories))
    total = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category IN ({qmarks}) AND {NOT_EXCLUDED}
          AND t.txn_date BETWEEN ? AND ?
        """,
        [*categories, start, end],
    ).fetchone()[0]
    return round(total, 2)


def period_bounds(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(min, max) txn_date across all transactions; (None, None) when empty."""
    row = conn.execute(
        "SELECT MIN(txn_date), MAX(txn_date) FROM transactions"
    ).fetchone()
    return row[0], row[1]


def unmapped_share(conn: sqlite3.Connection, side: str) -> float:
    """Fraction of non-excluded accounts on `side` ("BS" | "PL") whose
    category is NULL. An account whose side can't be determined (missing or
    unrecognized qbo_type) counts toward whichever side is asked —
    conservative: unknowns lower confidence everywhere."""
    rows = conn.execute(
        f"SELECT a.category, a.qbo_type FROM accounts a WHERE {NOT_EXCLUDED}"
    ).fetchall()
    total = unmapped = 0
    for r in rows:
        qbo_type = (r["qbo_type"] or "").strip().lower()
        if qbo_type in BALANCE_SHEET_TYPES:
            account_side = "BS"
        elif qbo_type in PL_TYPES:
            account_side = "PL"
        else:
            account_side = side
        if account_side != side:
            continue
        total += 1
        if r["category"] is None:
            unmapped += 1
    return unmapped / total if total else 0.0


def confidence_for(share: float) -> str:
    """0 → high, <=0.05 → medium, else low."""
    if share == 0:
        return "high"
    if share <= 0.05:
        return "medium"
    return "low"
