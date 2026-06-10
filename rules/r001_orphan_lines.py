"""R001 — journal lines posting to dead accounts.

WHY: a line whose account was deleted in QBO (or deactivated) is money
recorded against a bucket nobody looks at anymore — balances quietly
accumulate where no report surfaces them. The schema forbids a literal
NULL account_id (journal_lines.account_id is NOT NULL), so the real-world
orphan is a line referencing an account with qbo_deleted_at set or
active = false. Plumbing-proof rule: cheap, deterministic, obviously right.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R001"
severity: str = "warn"
title: str = "Lines posted to deleted/inactive accounts"
description: str = (
    "Journal lines whose account has been deleted in QBO or marked "
    "inactive. Postings to dead accounts escape every report built on "
    "active charts of accounts."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id,
               array_agg(DISTINCT a.name) AS dead_accounts,
               COUNT(jl.id) AS line_count
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        JOIN accounts a ON a.id = jl.account_id
        WHERE t.client_id = %s
          AND t.qbo_deleted_at IS NULL
          AND (t.txn_date IS NULL OR t.txn_date <= %s)
          AND (a.qbo_deleted_at IS NOT NULL OR a.active = false)
        GROUP BY t.id
        """,
        (client_id, as_of),
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "txn_type": txn_type,
                "qbo_id": qbo_id,
                "dead_accounts": sorted(dead_accounts),
                "line_count": line_count,
            },
        )
        for txn_id, txn_type, qbo_id, dead_accounts, line_count in rows
    ]
