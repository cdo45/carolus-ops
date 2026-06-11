"""R023 — open receivables concentrated in one customer.

WHY: for a contractor, one customer holding 40%+ of open A/R is an
existential cash-flow exposure — one slow payer (or one dispute on one
job) and payroll is at risk. Construction makes this worse: progress
billing piles receivables onto whichever job is mid-cycle. The rule
measures each customer's share of total open AR (net debit balance on
Accounts Receivable accounts by transaction entity) and fires above 50%
share AND $25,000 — both, so small books don't alarm on every invoice.

Recalibrated per controller audit: 40%/$10k → 50%/$25k and severity
info — concentration is structural context for the advisory call, not a
bookkeeping defect to fix this close.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R023"
severity: str = "info"
title: str = "A/R concentration risk"
description: str = (
    "A single customer holds > 50% of open accounts receivable and more "
    "than $25,000."
)

SHARE_NUM: int = 1  # net * 2 > total * 1  <=>  net/total > 0.5, exact
SHARE_DEN: int = 2
FLOOR: int = 25000


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        WITH ar AS (
            SELECT t.entity_id,
                   SUM(CASE WHEN jl.posting_type = 'debit'
                            THEN jl.amount ELSE -jl.amount END) AS net
            FROM journal_lines jl
            JOIN transactions t ON t.id = jl.transaction_id
            JOIN accounts a ON a.id = jl.account_id
            WHERE t.client_id = %(client_id)s
              AND a.acct_type = 'Accounts Receivable'
              AND t.entity_id IS NOT NULL
              AND t.txn_date <= %(as_of)s
              AND t.qbo_deleted_at IS NULL
            GROUP BY t.entity_id
        ),
        open_ar AS (SELECT * FROM ar WHERE net > 0),
        total AS (SELECT COALESCE(SUM(net), 0) AS total FROM open_ar)
        SELECT e.id, e.name, open_ar.net, total.total
        FROM open_ar
        JOIN entities e ON e.id = open_ar.entity_id
        CROSS JOIN total
        WHERE open_ar.net > %(floor)s
          AND open_ar.net * %(den)s > total.total * %(num)s
        """,
        {"client_id": client_id, "as_of": as_of, "floor": FLOOR,
         "num": SHARE_NUM, "den": SHARE_DEN},
    ).fetchall()
    return [
        Finding(
            source_type="entity",
            source_ref=str(entity_id),
            detail={
                "customer": name,
                "open_ar": str(net),
                "total_open_ar": str(total),
                "share_pct": str(round(net * 100 / total, 1)),
            },
        )
        for entity_id, name, net, total in rows
    ]
