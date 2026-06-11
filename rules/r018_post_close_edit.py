"""R018 — transactions edited after their period was closed GREEN.

WHY: a green close is a promise — those numbers were verified and may
already be in front of the owner, the bank, or the bonding agent. Any
transaction dated inside a green-closed period whose QBO LastUpdatedTime
is AFTER the close run finished means the verified books changed under
us: at best an unreviewed correction, at worst deliberate after-the-fact
rewriting. Either way the close's green is no longer trustworthy and the
period needs re-review. Severity critical.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R018"
severity: str = "critical"
title: str = "Edit after green close"
description: str = (
    "Transaction dated within a period whose close_runs status is green, "
    "modified in QBO (LastUpdatedTime) after that close was evaluated."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id, t.txn_date, t.qbo_synced_at,
               c.period_start, c.period_end, c.evaluated_at
        FROM close_runs c
        JOIN transactions t
          ON t.client_id = c.client_id
         AND t.txn_date BETWEEN c.period_start AND c.period_end
        WHERE c.client_id = %s
          AND c.status = 'green'
          AND t.qbo_synced_at IS NOT NULL
          AND t.qbo_synced_at > c.evaluated_at
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
        ORDER BY t.txn_date, t.qbo_id
        """,
        (client_id, as_of),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "modified_at": str(modified_at),
                "closed_period": f"{period_start} to {period_end}",
                "close_evaluated_at": str(evaluated_at),
            },
        )
        for txn_id, txn_type, qbo_id, txn_date, modified_at,
            period_start, period_end, evaluated_at in rows
    ]
