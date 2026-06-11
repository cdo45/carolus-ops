"""R025 — invoices open past 90 days.

WHY: receivables age like fish. Past 90 days, collection odds drop hard,
lien deadlines (construction's real leverage) may already be gone, and
the "open" balance often turns out to be a billing dispute nobody wrote
down. Each one is either money to chase or A/R to clean.

APPROXIMATION (documented, not silent): per-invoice open balance is read
from the latest STAGED payload's own `Balance` field — QBO's number, not
ours. The canonical link graph (payment applications) isn't extracted
yet; when it is, this rule should derive balances from applications.
Staging lag bounds the error at one sync cycle.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R025"
severity: str = "warn"
title: str = "Stale receivable"
description: str = (
    "Invoices older than 90 days still carrying an open balance >= $1,000 "
    "(balance read from QBO's own Balance field via staging)."
)

AGE_DAYS: int = 90
MIN_BALANCE: int = 1000


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    cutoff = as_of - timedelta(days=AGE_DAYS)
    rows = conn.execute(
        """
        SELECT t.id, t.qbo_id, t.txn_date, t.amount, staged.balance,
               COALESCE(e.name, '(no customer)')
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        JOIN LATERAL (
            SELECT (r.payload ->> 'Balance')::numeric AS balance
            FROM qbo_raw r
            WHERE r.client_id = t.client_id
              AND r.entity_type = t.txn_type
              AND r.qbo_id = t.qbo_id
              AND r.payload ? 'Balance'
            ORDER BY r.fetched_at DESC, r.id DESC
            LIMIT 1
        ) staged ON true
        WHERE t.client_id = %s
          AND t.txn_type = 'Invoice'
          AND t.txn_date < %s
          AND staged.balance >= %s
          AND t.qbo_deleted_at IS NULL
        ORDER BY t.txn_date, t.qbo_id
        """,
        (client_id, cutoff, MIN_BALANCE),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "amount": str(amount),
                "open_balance": str(balance.quantize(Decimal("0.01"))),
                "customer": customer,
                "age_days_over": AGE_DAYS,
            },
        )
        for txn_id, qbo_id, txn_date, amount, balance, customer in rows
    ]
