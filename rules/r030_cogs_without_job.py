"""R030 — job costs not attributed to any job.

WHY: for a contractor, an unattributed COGS dollar is a dollar of margin
error on EVERY job. Material and sub costs that don't carry a job tag
make profitable jobs look better and losing jobs invisible — the losing
job is the one bleeding the company while its costs hide in overhead.
This is the single most common construction bookkeeping failure and the
reason job-cost reports get ignored. The floor keeps shop consumables
out of the queue.

Recalibrated per controller audit: floor raised $250 -> $500; the
aggregate leak below $500/line is R034's job (ratio watch), not
per-transaction queue items.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R030"
severity: str = "warn"
title: str = "COGS posted without a job"
description: str = (
    "Cost-of-goods debit lines >= $500 with no job attribution. Every "
    "untagged cost dollar distorts the margin of every job."
)

THRESHOLD: int = 500


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id, t.txn_date,
               SUM(jl.amount) AS untagged_cogs,
               array_agg(DISTINCT a.name) AS cogs_accounts
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        JOIN accounts a ON a.id = jl.account_id
        WHERE t.client_id = %s
          AND a.acct_type = 'Cost of Goods Sold'
          AND jl.posting_type = 'debit'
          AND jl.job_id IS NULL
          AND jl.amount >= %s
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY t.id
        """,
        (client_id, THRESHOLD, as_of),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "txn_date": str(txn_date),
                "untagged_cogs": str(untagged),
                "cogs_accounts": sorted(accounts),
            },
        )
        for txn_id, txn_type, qbo_id, txn_date, untagged, accounts in rows
    ]
