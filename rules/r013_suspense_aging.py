"""R013 — aged balances in suspense/clearing/uncategorized accounts.

WHY: suspense and clearing accounts are parking spots, not destinations.
A balance composed of postings older than 30 days means somebody parked a
number and forgot it — misclassified expenses, unreconciled transfers, or
the bookkeeping equivalent of a junk drawer ("Ask My Accountant"). Every
dollar aging there is a dollar whose true account is unknown, and it
compounds straight into a messy year-end.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R013"
severity: str = "warn"
title: str = "Aged suspense/clearing balance"
description: str = (
    "Accounts named like suspense / clearing / ask my accountant / "
    "uncategorized whose postings older than 30 days net to a non-zero "
    "balance."
)

NAME_PATTERN: str = r"(suspense|clearing|ask my accountant|uncategorized)"
AGE_DAYS: int = 30


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    cutoff = as_of - timedelta(days=AGE_DAYS)
    rows = conn.execute(
        """
        SELECT a.id, a.name,
               SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) AS aged_net,
               MIN(t.txn_date) AS oldest_posting
        FROM accounts a
        JOIN journal_lines jl ON jl.account_id = a.id
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE a.client_id = %s
          AND a.name ~* %s
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY a.id
        HAVING SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) <> 0
        """,
        (client_id, NAME_PATTERN, cutoff),
    ).fetchall()
    return [
        Finding(
            source_type="account",
            source_ref=str(account_id),
            detail={
                "account": name,
                "aged_net": str(aged_net),
                "oldest_posting": str(oldest_posting),
                "age_threshold_days": AGE_DAYS,
            },
        )
        for account_id, name, aged_net, oldest_posting in rows
    ]
