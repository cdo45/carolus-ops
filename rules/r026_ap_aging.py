"""R026 — vendor bills unpaid past 60 days.

WHY: in construction, slow-paying a sub or supplier costs more than the
money: supply-house credit holds, crews that won't show, and preliminary
lien notices landing on YOUR customer's project. A bill aging past 60
days is either a cash problem (the owner must know), a disputed bill
(write the dispute down), or an entry error (a payment recorded outside
A/P). Same staged-Balance approximation as R025, same documented reason.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R026"
severity: str = "warn"
title: str = "Aged payable"
description: str = (
    "Bills older than 60 days still carrying an open balance >= $1,000 "
    "(balance read from QBO's own Balance field via staging)."
)

AGE_DAYS: int = 60
MIN_BALANCE: int = 1000


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    cutoff = as_of - timedelta(days=AGE_DAYS)
    rows = conn.execute(
        """
        SELECT t.id, t.qbo_id, t.txn_date, t.amount, staged.balance,
               COALESCE(e.name, '(no vendor)')
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
          AND t.txn_type = 'Bill'
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
                "vendor": vendor,
                "age_days_over": AGE_DAYS,
            },
        )
        for txn_id, qbo_id, txn_date, amount, balance, vendor in rows
    ]
