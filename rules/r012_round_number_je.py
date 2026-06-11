"""R012 — journal entries with large round-number lines.

WHY: real transactions almost never land on $5,000.00 exactly; manual
plugs, estimates, and fabricated adjustments do. A JE line of $1,000 or
more ending in 000 is the auditor's oldest tell for "someone made this
number up" — period-smoothing, balance plugs, or worse. Usually benign
(accruals), which is why it is a warn, but every one deserves an answer
to "where did this number come from?".

Recalibrated per controller audit: floor raised $1,000 -> $5,000 — the
$1k-$4k band was dominated by legitimate small accruals.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R012"
severity: str = "warn"
title: str = "Round-number journal entry"
description: str = (
    "JournalEntry lines >= $5,000 that are exact multiples of 1,000 — "
    "the classic signature of plugged or estimated numbers."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.qbo_id, t.txn_date,
               array_agg(jl.amount::text ORDER BY jl.line_no) AS round_amounts
        FROM transactions t
        JOIN journal_lines jl ON jl.transaction_id = t.id
        WHERE t.client_id = %s
          AND t.txn_type = 'JournalEntry'
          AND t.qbo_deleted_at IS NULL
          AND (t.txn_date IS NULL OR t.txn_date <= %s)
          AND jl.amount >= 5000
          AND jl.amount %% 1000 = 0
        GROUP BY t.id
        """,
        (client_id, as_of),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "round_amounts": round_amounts,
            },
        )
        for txn_id, qbo_id, txn_date, round_amounts in rows
    ]
