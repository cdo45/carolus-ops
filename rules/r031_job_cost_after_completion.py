"""R031 — costs landing on jobs that are already done.

WHY: a completed job's margin is supposed to be final — it was (or will
be) reported to the owner, used in pricing the next bid, maybe certified.
Costs that keep arriving after completion either belong to a DIFFERENT
job (misallocation hiding a loss elsewhere), are warranty/rework that
should be tracked as such, or are padding a closed cost code. Any of
those silently rewrites a number someone already relied on.

Completion is the canonical jobs.status in ('completed', 'closed') —
curated by Carlos, since QBO has no native job-completion state. Until a
completed_at date exists, ANY cost on a completed job fires.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R031"
severity: str = "warn"
title: str = "Cost posted to completed job"
description: str = (
    "Cost lines attributed to jobs whose status is completed/closed — "
    "the job's final margin is being rewritten after the fact."
)

DONE_STATUSES: tuple[str, ...] = ("completed", "closed")


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id, t.txn_date, j.name, j.status,
               SUM(jl.amount) AS late_cost
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        JOIN jobs j ON j.id = jl.job_id
        JOIN accounts a ON a.id = jl.account_id
        WHERE t.client_id = %s
          AND j.status = ANY(%s)
          AND jl.posting_type = 'debit'
          AND a.acct_type IN ('Cost of Goods Sold', 'Expense', 'Other Expense')
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY t.id, j.id
        """,
        (client_id, list(DONE_STATUSES), as_of),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "job": job_name,
                "job_status": job_status,
                "late_cost": str(late_cost),
            },
        )
        for txn_id, txn_type, qbo_id, txn_date, job_name, job_status, late_cost
        in rows
    ]
