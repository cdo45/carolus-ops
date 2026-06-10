"""R016 — entries created long after the date they claim.

WHY: an entry keyed in 45+ days after its transaction date is rewriting
history — late corrections pushed into a prior (possibly already
reviewed) period, smoothing between months, or covering something up.
Compares QBO's own CreateTime metadata (when the entry was actually
typed) against txn_date (when it claims to have happened); both are
content-derived from the raw payload. Legitimate catch-up bookkeeping
fires this too — that pattern is itself worth a conversation.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R016"
severity: str = "warn"
title: str = "Backdated entry"
description: str = (
    "Transactions whose QBO CreateTime is more than 45 days after their "
    "stated txn_date — late edits into closed or reviewed periods."
)

BACKDATE_DAYS: int = 45


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT id, txn_type, qbo_id, txn_date,
               (qbo_created_at AT TIME ZONE 'UTC')::date AS created_date,
               (qbo_created_at AT TIME ZONE 'UTC')::date - txn_date AS days_late
        FROM transactions
        WHERE client_id = %s
          AND qbo_created_at IS NOT NULL
          AND txn_date IS NOT NULL
          AND txn_date <= %s
          AND qbo_deleted_at IS NULL
          AND (qbo_created_at AT TIME ZONE 'UTC')::date - txn_date > %s
        """,
        (client_id, as_of, BACKDATE_DAYS),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "created_date": str(created_date),
                "days_late": days_late,
            },
        )
        for txn_id, txn_type, qbo_id, txn_date, created_date, days_late in rows
    ]
