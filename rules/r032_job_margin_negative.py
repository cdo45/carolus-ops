"""R032 — jobs whose costs have passed their billings.

WHY: this is THE number a construction owner pays an accountant to watch.
A job with billings whose job-to-date costs exceed job-to-date income is
underwater right now — underbid, over-bought, or underbilled. Caught
mid-job it's recoverable (change order, rebill, stop the bleeding);
caught at year-end it's just a loss with a story. Only jobs with ANY
billing fire — a job that hasn't billed yet legitimately runs cost-heavy.

Recalibrated per controller audit — split severity: CRITICAL only when
costs exceed 110% of billings AND the first job cost is >= 45 days old
(deep underwater on a mature job = real loss forming); a margin that is
merely negative, or on a job too young to have billed its costs through,
is INFO — normal billing lag, watch not act. An open flag keeps the
severity it was born with (the natural key is the job); it re-grades on
the next occurrence after resolution.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R032"
severity: str = "critical"  # module default; per-finding override below
title: str = "Job margin negative"
description: str = (
    "Job-to-date income minus job-to-date costs is negative for a job "
    "with billings. Critical when costs > 110% of billings and the first "
    "cost is >= 45 days old; info otherwise (billing lag)."
)

COST_TYPES: tuple[str, ...] = ("Cost of Goods Sold", "Expense", "Other Expense")
CRITICAL_COST_PCT_NUM: int = 11  # costs * 10 > income * 11 <=> costs > 110%
CRITICAL_COST_PCT_DEN: int = 10
MATURITY_DAYS: int = 45


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
                   ELSE 0 END), 0) AS costs,
               MIN(CASE WHEN a.acct_type = ANY(%(cost_types)s)
                         AND jl.posting_type = 'debit'
                        THEN t.txn_date END) AS first_cost_date
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

    maturity_cutoff = as_of - timedelta(days=MATURITY_DAYS)
    findings: list[Finding] = []
    for job_id, name, income, costs, first_cost_date in rows:
        deep = costs * CRITICAL_COST_PCT_DEN > income * CRITICAL_COST_PCT_NUM
        mature = first_cost_date is not None and first_cost_date <= maturity_cutoff
        grade = "critical" if deep and mature else "info"
        findings.append(Finding(
            source_type="job",
            source_ref=str(job_id),
            severity=grade,
            detail={
                "job": name,
                "income_to_date": str(income),
                "costs_to_date": str(costs),
                "margin": str(income - costs),
                "first_cost_date": str(first_cost_date),
                "grade": grade,
            },
        ))
    return findings
