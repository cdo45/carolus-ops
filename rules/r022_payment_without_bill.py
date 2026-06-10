"""R022 — bill payments not applied to any bill.

WHY: a BillPayment with no linked Bill means money left under "paying
bills" without a bill to pay — either the bill was never entered (so the
expense is missing from the P&L), or the application was deleted, or AP
is being used as a pass-through. Mostly workflow sloppiness rather than
fraud, hence info severity, but each one leaves A/P misstated until the
application is fixed.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R022"
severity: str = "info"
title: str = "Bill payment without linked bill"
description: str = (
    "BillPayments whose payload links to no Bill (has_linked_txn = false). "
    "A/P is misstated until the payment is applied."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.qbo_id, t.txn_date, t.amount, e.name
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %s
          AND t.txn_type = 'BillPayment'
          AND t.has_linked_txn IS FALSE
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
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
                "amount": str(amount),
                "vendor": vendor,
            },
        )
        for txn_id, qbo_id, txn_date, amount, vendor in rows
    ]
