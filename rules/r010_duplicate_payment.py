"""R010 — possible duplicate payment: same vendor, same amount, close together.

WHY: the classic cash-leakage error. A vendor invoice gets entered twice
(once from the email, once from the month-end statement), or a bill is
paid by check AND again by card, and the vendor quietly keeps both. Pairs
of money-out transactions (Bill / BillPayment / Purchase) to the same
vendor for the same amount over $500 within 10 days, under different
qbo_ids, are almost never intentional. The Bill+BillPayment combination
is excluded: a bill followed by its own payment at the same amount is the
NORMAL flow, not a duplicate. Severity critical — real hits are money
already out the door; recovery gets harder by the week.

Recalibrated per controller audit: floor raised $100 -> $500 (the
sub-$500 band was all small-purchase noise) and vendors with >= 3
identical-amount transactions in the trailing 12 months are suppressed —
that cadence is a subscription/recurring charge, not a duplicate.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R010"
severity: str = "critical"
title: str = "Possible duplicate payment"
description: str = (
    "Two money-out transactions (Bill/BillPayment/Purchase) to the same "
    "vendor for the same amount > $500 within 10 days under different "
    "qbo_ids. Bill+BillPayment pairs and recurring-charge vendors "
    "(>= 3 identical amounts in trailing 12 months) are excluded."
)

_SPEND_TYPES = ("Bill", "BillPayment", "Purchase")
FLOOR: int = 500
RECURRING_COUNT: int = 3  # this many identical amounts/12mo = subscription


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT later.id, later.txn_type, later.qbo_id, later.amount,
               array_agg(earlier.txn_type || ':' || earlier.qbo_id
                         ORDER BY earlier.txn_date, earlier.qbo_id) AS matches,
               MAX(later.txn_date - earlier.txn_date) AS max_days_apart
        FROM transactions later
        JOIN transactions earlier
          ON earlier.client_id = later.client_id
         AND earlier.entity_id = later.entity_id
         AND earlier.amount = later.amount
         AND earlier.qbo_id <> later.qbo_id
         AND earlier.txn_date <= later.txn_date
         AND later.txn_date - earlier.txn_date <= 10
         AND (earlier.txn_date < later.txn_date
              OR earlier.qbo_id < later.qbo_id)
         AND earlier.txn_type = ANY(%(types)s)
         AND NOT ((earlier.txn_type = 'Bill' AND later.txn_type = 'BillPayment')
               OR (earlier.txn_type = 'BillPayment' AND later.txn_type = 'Bill'))
         AND earlier.qbo_deleted_at IS NULL
        WHERE later.client_id = %(client_id)s
          AND later.txn_type = ANY(%(types)s)
          AND later.amount > %(floor)s
          AND later.entity_id IS NOT NULL
          AND later.txn_date <= %(as_of)s
          AND later.qbo_deleted_at IS NULL
          AND (
              SELECT count(*) FROM transactions recurring
              WHERE recurring.client_id = later.client_id
                AND recurring.entity_id = later.entity_id
                AND recurring.amount = later.amount
                AND recurring.txn_type = ANY(%(types)s)
                AND recurring.txn_date
                    BETWEEN %(as_of)s::date - 365 AND %(as_of)s
                AND recurring.qbo_deleted_at IS NULL
          ) < %(recurring)s
        GROUP BY later.id
        """,
        {"client_id": client_id, "types": list(_SPEND_TYPES), "as_of": as_of,
         "floor": FLOOR, "recurring": RECURRING_COUNT},
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "amount": str(amount),
                "matches": matches,
                "max_days_apart": max_days_apart,
            },
        )
        for txn_id, txn_type, qbo_id, amount, matches, max_days_apart in rows
    ]
