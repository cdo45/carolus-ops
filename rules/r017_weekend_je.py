"""R017 — journal entries dated on weekends.

WHY: journal entries in QBO are by definition manual bookkeeping actions,
and bookkeeping happens on business days. A JE *dated* Saturday or Sunday
is out of rhythm: sometimes innocent (owner catching up), but weekend
dates are also where people park entries they'd rather not have noticed
in the Monday-morning review, and a known marker in fraud casework.
Info severity — a pattern to watch, not an alarm.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R017"
severity: str = "info"
title: str = "Weekend journal entry"
description: str = (
    "Manual JournalEntries dated on a Saturday or Sunday — out-of-rhythm "
    "entries worth a glance in review."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT id, qbo_id, txn_date, amount,
               trim(to_char(txn_date, 'Day')) AS day_name
        FROM transactions
        WHERE client_id = %s
          AND txn_type = 'JournalEntry'
          AND txn_date IS NOT NULL
          AND txn_date <= %s
          AND EXTRACT(ISODOW FROM txn_date) IN (6, 7)
          AND qbo_deleted_at IS NULL
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
                "day": day_name,
                "amount": str(amount),
            },
        )
        for txn_id, qbo_id, txn_date, amount, day_name in rows
    ]
