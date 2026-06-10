"""R032 — jobs whose costs have passed their billings.

WHY: this is THE number a construction owner pays an accountant to watch.
A job with billings whose job-to-date costs exceed job-to-date income is
underwater right now — underbid, over-bought, or underbilled. Caught
mid-job it's recoverable (change order, rebill, stop the bleeding);
caught at year-end it's just a loss with a story. Only jobs with ANY
billing fire — a job that hasn't billed yet legitimately runs cost-heavy.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R032"
severity: str = "critical"
title: str = "Job margin negative"
description: str = (
    "Job-to-date income minus job-to-date costs is negative for a job "
    "with billings. The job is underwater while it can still be fixed."
)

COST_TYPES: tuple[str, ...] = ("Cost of Goods Sold", "Expense", "Other Expense")


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT j.id, j.name,
               COALESCE(SUM(CASE WHEN a.acct_type = 'Income' THEN
                   CASE WHEN jl.posting_type = 'credit'
                        THEN jl.amount ELSE -jl.amount END
                   ELSE 0 END), 0) AS income,
               COALESCE(SUM(CASE WHEN a.acct_type = ANY(%(cost_types)s) THEN
                   CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END
                   ELSE 0 END), 0) AS costs
        FROM jobs j
        JOIN journal_lines jl ON jl.job_id = j.id
        JOIN transactions t ON t.id = jl.transaction_id
        JOIN accounts a ON a.id = jl.account_id
        WHERE j.client_id = %(client_id)s
          AND t.txn_date <= %(as_of)s
          AND t.qbo_deleted_at IS NULL
        GROUP BY j.id
        HAVING COALESCE(SUM(CASE WHEN a.acct_type = 'Income' THEN
                   CASE WHEN jl.posting_type = 'credit'
                        THEN jl.amount ELSE -jl.amount END
                   ELSE 0 END), 0) > 0
           AND COALESCE(SUM(CASE WHEN a.acct_type = 'Income' THEN
                   CASE WHEN jl.posting_type = 'credit'
                        THEN jl.amount ELSE -jl.amount END
                   ELSE 0 END), 0)
             < COALESCE(SUM(CASE WHEN a.acct_type = ANY(%(cost_types)s) THEN
                   CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END
                   ELSE 0 END), 0)
        """,
        {"client_id": client_id, "as_of": as_of, "cost_types": list(COST_TYPES)},
    ).fetchall()
    return [
        Finding(
            source_type="job",
            source_ref=str(job_id),
            detail={
                "job": name,
                "income_to_date": str(income),
                "costs_to_date": str(costs),
                "margin": str(income - costs),
            },
        )
        for job_id, name, income, costs in rows
    ]
