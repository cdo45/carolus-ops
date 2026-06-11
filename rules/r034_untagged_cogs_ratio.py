"""R034 — the month's untagged-COGS leak, measured as a ratio.

WHY: R030 catches big single leaks (>= $500/line); this catches the slow
bleed — fifty small purchases that individually duck the floor but
together corrupt every job margin. Above 5% untagged, job-cost reports
are decoration. ONE summary flag per client+month (the fix is process —
"tag at point of entry" — not fifty queue items). The $5,000 denominator
floor keeps tiny months from screaming over $80 of drill bits.

Finding identity: source is the CLIENT row (the condition is book-wide);
the month lives in the detail. Consecutive bad months keep the one open
flag; after it resolves, a later bad month re-fires as a new occurrence.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R034"
severity: str = "critical"
title: str = "Untagged COGS above ratio"
description: str = (
    "Month's untagged COGS exceeds 5% of total COGS (total >= $5,000) — "
    "the aggregate leak that corrupts every job margin."
)

RATIO_NUM: int = 1  # untagged * 20 > total * 1  <=>  untagged/total > 5%
RATIO_DEN: int = 20
MIN_TOTAL: int = 5000


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    month_start = as_of.replace(day=1)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(jl.amount) FILTER (WHERE jl.job_id IS NULL), 0)
                   AS untagged,
               COALESCE(SUM(jl.amount), 0) AS total
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        JOIN accounts a ON a.id = jl.account_id
        WHERE t.client_id = %s
          AND a.acct_type = 'Cost of Goods Sold'
          AND jl.posting_type = 'debit'
          AND t.txn_date BETWEEN %s AND %s
          AND t.qbo_deleted_at IS NULL
        """,
        (client_id, month_start, as_of),
    ).fetchone()
    assert row is not None
    untagged, total = row
    if total < MIN_TOTAL or untagged * RATIO_DEN <= total * RATIO_NUM:
        return []
    ratio_pct = (untagged * 100 / total).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)
    return [Finding(
        source_type="client",
        source_ref=str(client_id),
        detail={
            "month": month_start.strftime("%Y-%m"),
            "untagged_cogs": str(untagged),
            "total_cogs": str(total),
            "untagged_pct": str(ratio_pct),
        },
    )]
