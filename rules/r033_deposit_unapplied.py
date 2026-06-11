"""R033 — customer money received but never applied to anything.

WHY: deposit and retainage hygiene. A customer Payment sitting unapplied
for a month means either an invoice was never raised (revenue and the
customer's statement are both wrong), the cash was a job deposit that
should be tracked as a liability until earned, or A/R aging is overstated
and someone will dun a customer who already paid. In construction —
deposits up front, retainage at the end — unapplied cash is routine and
routinely mishandled.

Scope (P1 link data): fires on Payment transactions with
has_linked_txn = false older than 30 days. Unapplied CreditMemos aren't
visible from canonical link data yet — documented gap, revisit when the
link graph is extracted.

Recalibrated per controller audit: $500 minimum — small unapplied
remnants are change/rounding on partial payments, not deposit handling.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R033"
severity: str = "warn"
title: str = "Unapplied customer payment"
description: str = (
    "Customer Payments >= $500 applied to no invoice and older than 30 "
    "days — deposit/retainage hygiene; A/R and revenue are both suspect."
)

AGE_DAYS: int = 30
MIN_AMOUNT: int = 500


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    cutoff = as_of - timedelta(days=AGE_DAYS)
    rows = conn.execute(
        """
        SELECT t.id, t.qbo_id, t.txn_date, t.amount, e.name
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %s
          AND t.txn_type = 'Payment'
          AND t.has_linked_txn IS FALSE
          AND t.amount >= %s
          AND t.txn_date < %s
          AND t.qbo_deleted_at IS NULL
        """,
        (client_id, MIN_AMOUNT, cutoff),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "amount": str(amount),
                "customer": customer,
                "age_threshold_days": AGE_DAYS,
            },
        )
        for txn_id, qbo_id, txn_date, amount, customer in rows
    ]
