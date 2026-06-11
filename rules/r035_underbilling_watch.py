"""R035 — jobs burning cost with no billing going out.

WHY: underbilling is how contractors finance their customers by accident.
A job actively taking costs in the last 30 days, with $10k+ sunk and not
one invoice out in the same window, is floating someone else's project on
the company's cash. Usually it's a progress bill nobody drafted; the
watch exists so "we'll bill it next month" gets caught THIS month.

Billing attribution follows job-tagged journal lines (income lines carry
job_id), consistent with R032 — an invoice to the job's parent customer
without job-tagged lines does not count as billing the job, which is
itself a tagging problem worth surfacing.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R035"
severity: str = "warn"
title: str = "Underbilling watch"
description: str = (
    "Job with cost activity in the trailing 30 days, >= $10,000 "
    "job-to-date costs, and zero invoices in the trailing 30 days."
)

WINDOW_DAYS: int = 30
MIN_JTD_COSTS: int = 10000
COST_TYPES: tuple[str, ...] = ("Cost of Goods Sold", "Expense", "Other Expense")


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    window_start = as_of - timedelta(days=WINDOW_DAYS)
    rows = conn.execute(
        """
        SELECT j.id, j.name,
               SUM(jl.amount) FILTER (WHERE jl.posting_type = 'debit')
                   AS jtd_costs,
               MAX(t.txn_date) FILTER (WHERE jl.posting_type = 'debit')
                   AS last_cost_date
        FROM jobs j
        JOIN journal_lines jl ON jl.job_id = j.id
        JOIN transactions t ON t.id = jl.transaction_id
        JOIN accounts a ON a.id = jl.account_id
        WHERE j.client_id = %(client_id)s
          AND a.acct_type = ANY(%(cost_types)s)
          AND t.txn_date <= %(as_of)s
          AND t.qbo_deleted_at IS NULL
        GROUP BY j.id
        HAVING SUM(jl.amount) FILTER (WHERE jl.posting_type = 'debit')
                   >= %(min_costs)s
           AND MAX(t.txn_date) FILTER (WHERE jl.posting_type = 'debit')
                   >= %(window_start)s
           AND NOT EXISTS (
               SELECT 1
               FROM journal_lines bill_line
               JOIN transactions bill_txn
                 ON bill_txn.id = bill_line.transaction_id
               WHERE bill_line.job_id = j.id
                 AND bill_txn.txn_type = 'Invoice'
                 AND bill_txn.txn_date BETWEEN %(window_start)s AND %(as_of)s
                 AND bill_txn.qbo_deleted_at IS NULL
           )
        """,
        {"client_id": client_id, "as_of": as_of,
         "window_start": window_start, "min_costs": MIN_JTD_COSTS,
         "cost_types": list(COST_TYPES)},
    ).fetchall()
    return [
        Finding(
            source_type="job",
            source_ref=str(job_id),
            detail={
                "job": name,
                "jtd_costs": str(jtd_costs),
                "last_cost_date": str(last_cost_date),
                "window_days": WINDOW_DAYS,
            },
        )
        for job_id, name, jtd_costs, last_cost_date in rows
    ]
