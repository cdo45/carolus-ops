"""R024 — meaningful spend with no vendor attached.

WHY: a $500+ purchase or bill with no vendor is unattributable money out:
it breaks 1099 totals, vendor spend reports, duplicate detection (R010
keys on vendor), and job costing. Usually a bank-feed entry accepted
without coding. Every spend transaction above the floor must name who got
paid.

Recalibrated per controller audit: Purchases whose raw EntityRef names an
Employee are exempt — employees aren't synced until P8 (payroll), so the
canonical entity_id is legitimately NULL; the payee IS named in QBO. Read
from the staged payload, the source of truth for what QBO actually holds.
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
        SELECT t.id, t.qbo_id, t.txn_type, t.txn_date, t.amount
        FROM transactions t
        WHERE t.client_id = %s
          AND t.txn_type IN ('Purchase', 'Bill')
          AND t.entity_id IS NULL
          AND t.amount >= %s
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
          AND NOT EXISTS (
              -- employee payees exist in QBO but not in canonical until P8
              SELECT 1 FROM qbo_raw r
              WHERE r.client_id = t.client_id
                AND r.entity_type = t.txn_type
                AND r.qbo_id = t.qbo_id
                AND lower(COALESCE(r.payload #>> '{EntityRef,type}',
                                   r.payload #>> '{EntityRef,Type}'))
                    = 'employee'
          )
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
