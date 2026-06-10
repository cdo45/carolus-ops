"""R020 — vendor spend spiking far above its own history.

WHY: a vendor that normally bills a few hundred a month suddenly billing
thousands is how busted budgets, duplicate invoicing, scope creep, and
vendor fraud all first show up. Comparing each vendor to ITS OWN trailing
six-month average (not a fixed threshold) keeps the signal meaningful for
both small and large vendors; the $2,500 floor keeps trivia out. Spend is
measured on expense-recording transactions (Bill, Purchase) — BillPayments
are excluded so paying an old bill doesn't double-count.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R020"
severity: str = "warn"
title: str = "Vendor spend spike"
description: str = (
    "Vendor's current-month spend exceeds 2.5x its trailing-6-month "
    "average AND $2,500. A brand-new vendor with no history trips this "
    "on any month over $2,500 (see also R021)."
)

SPIKE_NUM: int = 12  # month_spend * 12 > trailing_total * 5  <=>
SPIKE_DEN: int = 5  # month_spend > 2.5 * (trailing_total / 6), exact in SQL
FLOOR: int = 2500


def months_back(day: date, months: int) -> date:
    """First day of the month `months` before day's month."""
    total = day.year * 12 + (day.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    month_start = as_of.replace(day=1)
    trailing_start = months_back(as_of, 6)
    rows = conn.execute(
        """
        SELECT e.id, e.name,
               COALESCE(SUM(t.amount) FILTER (
                   WHERE t.txn_date >= %(month_start)s), 0) AS month_spend,
               COALESCE(SUM(t.amount) FILTER (
                   WHERE t.txn_date < %(month_start)s), 0) AS trailing_total
        FROM entities e
        JOIN transactions t ON t.entity_id = e.id AND t.client_id = e.client_id
        WHERE e.client_id = %(client_id)s
          AND e.kind = 'vendor'
          AND t.txn_type IN ('Bill', 'Purchase')
          AND t.txn_date >= %(trailing_start)s
          AND t.txn_date <= %(as_of)s
          AND t.qbo_deleted_at IS NULL
        GROUP BY e.id
        HAVING COALESCE(SUM(t.amount) FILTER (
                   WHERE t.txn_date >= %(month_start)s), 0) > %(floor)s
           AND COALESCE(SUM(t.amount) FILTER (
                   WHERE t.txn_date >= %(month_start)s), 0) * %(num)s
             > COALESCE(SUM(t.amount) FILTER (
                   WHERE t.txn_date < %(month_start)s), 0) * %(den)s
        """,
        {
            "client_id": client_id,
            "month_start": month_start,
            "trailing_start": trailing_start,
            "as_of": as_of,
            "floor": FLOOR,
            "num": SPIKE_NUM,
            "den": SPIKE_DEN,
        },
    ).fetchall()
    return [
        Finding(
            source_type="entity",
            source_ref=str(entity_id),
            detail={
                "vendor": name,
                "month_spend": str(month_spend),
                "trailing_6mo_total": str(trailing_total),
                "trailing_6mo_avg": str(round(trailing_total / 6, 2)),
                "month_start": month_start.isoformat(),
            },
        )
        for entity_id, name, month_spend, trailing_total in rows
    ]
