"""R024 — meaningful spend with no vendor attached.

WHY: a $500+ purchase or bill with no vendor is unattributable money out:
it breaks 1099 totals, vendor spend reports, duplicate detection (R010
keys on vendor), and job costing. Usually a bank-feed entry accepted
without coding. Every spend transaction above the floor must name who got
paid.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R024"
severity: str = "warn"
title: str = "Spend without vendor"
description: str = (
    "Purchases/Bills >= $500 with no entity attached — unattributable "
    "money out; breaks 1099s, vendor reporting, and duplicate detection."
)

THRESHOLD: int = 500


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT id, qbo_id, txn_type, txn_date, amount
        FROM transactions
        WHERE client_id = %s
          AND txn_type IN ('Purchase', 'Bill')
          AND entity_id IS NULL
          AND amount >= %s
          AND txn_date <= %s
          AND qbo_deleted_at IS NULL
        """,
        (client_id, THRESHOLD, as_of),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "qbo_id": qbo_id,
                "txn_type": txn_type,
                "txn_date": str(txn_date),
                "amount": str(amount),
            },
        )
        for txn_id, qbo_id, txn_type, txn_date, amount in rows
    ]
