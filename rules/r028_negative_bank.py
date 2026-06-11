"""R028 — bank account underwater on the books at month-end.

WHY: a negative book balance on a bank account means either the company
is actually overdrawn (cash emergency, NSF fees compounding) or the books
have unrecorded deposits / double-recorded payments (the balance is a
lie). Both demand same-day attention. Month-end is the measurement point
because that's the number statements and the close certify.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R028"
severity: str = "critical"
title: str = "Negative bank balance"
description: str = (
    "Bank-type account whose book balance (debits - credits) is below "
    "zero as of the as_of month-end."
)


def month_end(day: date) -> date:
    next_month = (date(day.year + 1, 1, 1) if day.month == 12
                  else date(day.year, day.month + 1, 1))
    return next_month - timedelta(days=1)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    cutoff = month_end(as_of)
    rows = conn.execute(
        """
        SELECT a.id, a.name,
               SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) AS balance
        FROM accounts a
        JOIN journal_lines jl ON jl.account_id = a.id
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE a.client_id = %s
          AND a.acct_type = 'Bank'
          AND a.qbo_deleted_at IS NULL
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY a.id
        HAVING SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) < 0
        """,
        (client_id, cutoff),
    ).fetchall()
    return [
        Finding(
            source_type="account",
            source_ref=str(account_id),
            detail={
                "account": name,
                "book_balance": str(balance),
                "as_of_month_end": cutoff.isoformat(),
            },
        )
        for account_id, name, balance in rows
    ]
