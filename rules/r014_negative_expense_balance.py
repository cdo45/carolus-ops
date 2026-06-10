"""R014 — expense accounts running a credit balance for the period.

WHY: expenses normally carry debit balances. A net CREDIT balance for the
month usually means a refund or vendor credit was posted to the expense
account without the original charge being there, a reversal was entered
twice, or income was misposted into an expense line. Each one distorts
both the P&L and any job costing built on it. Period = the calendar
month containing as_of (the month being closed).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R014"
severity: str = "warn"
title: str = "Expense account with credit-side period balance"
description: str = (
    "Expense-type accounts whose postings in the as_of month net to the "
    "credit side — likely misposted refunds, double reversals, or "
    "misclassified income."
)

EXPENSE_TYPES: tuple[str, ...] = ("Expense", "Other Expense", "Cost of Goods Sold")


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    period_start = as_of.replace(day=1)
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.acct_type,
               SUM(CASE WHEN jl.posting_type = 'credit'
                        THEN jl.amount ELSE -jl.amount END) AS net_credit
        FROM accounts a
        JOIN journal_lines jl ON jl.account_id = a.id
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE a.client_id = %s
          AND a.acct_type = ANY(%s)
          AND t.txn_date BETWEEN %s AND %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY a.id
        HAVING SUM(CASE WHEN jl.posting_type = 'credit'
                        THEN jl.amount ELSE -jl.amount END) > 0
        """,
        (client_id, list(EXPENSE_TYPES), period_start, as_of),
    ).fetchall()
    return [
        Finding(
            source_type="account",
            source_ref=str(account_id),
            detail={
                "account": name,
                "acct_type": acct_type,
                "net_credit": str(net_credit),
                "period_start": period_start.isoformat(),
                "period_end": as_of.isoformat(),
            },
        )
        for account_id, name, acct_type, net_credit in rows
    ]
