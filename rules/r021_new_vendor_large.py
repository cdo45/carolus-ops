"""R021 — first-ever transaction with a vendor is already big money.

WHY: legitimate vendor relationships usually ramp: small order, then
bigger. A relationship that OPENS at $5,000+ deserves a look — it is the
signature of fake-vendor schemes (set up a shell, bill once, big), of
unvetted commitments, and occasionally just of a typo'd vendor name
splitting history (which also breaks 1099 reporting). Flags the first
transaction itself so review starts at the document.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R021"
severity: str = "warn"
title: str = "Large first payment to new vendor"
description: str = (
    "The first-ever money-out transaction (Bill/Purchase/BillPayment) "
    "for a vendor is >= $5,000."
)

THRESHOLD: int = 5000


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT first.id, first.qbo_id, first.txn_type, first.txn_date,
               first.amount, first.vendor_name
        FROM (
            SELECT DISTINCT ON (t.entity_id)
                   t.id, t.qbo_id, t.txn_type, t.txn_date, t.amount,
                   e.name AS vendor_name
            FROM transactions t
            JOIN entities e ON e.id = t.entity_id
            WHERE t.client_id = %s
              AND e.kind = 'vendor'
              AND t.txn_type IN ('Bill', 'Purchase', 'BillPayment')
              AND t.txn_date <= %s
              AND t.qbo_deleted_at IS NULL
            ORDER BY t.entity_id, t.txn_date, t.qbo_id
        ) first
        WHERE first.amount >= %s
        """,
        (client_id, as_of, THRESHOLD),
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
                "vendor": vendor_name,
            },
        )
        for txn_id, qbo_id, txn_type, txn_date, amount, vendor_name in rows
    ]
