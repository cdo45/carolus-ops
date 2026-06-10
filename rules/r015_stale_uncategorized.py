"""R015 — transactions sitting in Uncategorized accounts for 14+ days.

WHY: QBO's bank feeds park unmatched activity in "Uncategorized Expense /
Income / Asset". A few days there is workflow; two weeks is abandonment —
the P&L is silently wrong by exactly those amounts, and the longer they
sit the harder the "what was this $317 at Home Depot?" conversation gets.
Fourteen days is the line between in-progress and forgotten.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R015"
severity: str = "warn"
title: str = "Stale uncategorized transaction"
description: str = (
    "Transactions with lines in Uncategorized* accounts older than 14 "
    "days — bank-feed activity nobody categorized."
)

STALE_DAYS: int = 14


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    cutoff = as_of - timedelta(days=STALE_DAYS)
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id, t.txn_date,
               array_agg(DISTINCT a.name) AS uncategorized_accounts,
               SUM(jl.amount) AS total_amount
        FROM transactions t
        JOIN journal_lines jl ON jl.transaction_id = t.id
        JOIN accounts a ON a.id = jl.account_id
        WHERE t.client_id = %s
          AND a.name ILIKE 'uncategorized%%'
          AND t.txn_date < %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY t.id
        """,
        (client_id, cutoff),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "uncategorized_accounts": sorted(accounts),
                "total_amount": str(total_amount),
                "stale_after_days": STALE_DAYS,
            },
        )
        for txn_id, txn_type, qbo_id, txn_date, accounts, total_amount in rows
    ]
