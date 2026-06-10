"""R000 — transactions whose journal lines do not net to zero.

WHY: debits = credits is the bedrock invariant of double-entry books. A
transaction that nets non-zero (or has an amount but no lines at all)
means the transform could not fully map it — unresolved accounts, unmapped
sales tax — or the books themselves are corrupt. Anything downstream
(margins, KPIs, close) silently lies until these are cleared. This rule
surfaces the same conditions sync marks as transform_warning, but as
engine-managed flags with the full open/auto-resolve lifecycle.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R000"
severity: str = "warn"
title: str = "Unbalanced journal lines"
description: str = (
    "Transactions whose journal lines do not sum to zero net (debits != "
    "credits), or that carry an amount with no lines at all. These books "
    "cannot be trusted for analysis until mapped or corrected."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id,
               COALESCE(SUM(CASE WHEN jl.posting_type = 'debit'
                                 THEN jl.amount ELSE -jl.amount END), 0) AS net,
               COUNT(jl.id) AS line_count,
               COALESCE(t.amount, 0) AS amount
        FROM transactions t
        LEFT JOIN journal_lines jl ON jl.transaction_id = t.id
        WHERE t.client_id = %s
          AND t.qbo_deleted_at IS NULL
          AND (t.txn_date IS NULL OR t.txn_date <= %s)
        GROUP BY t.id
        HAVING COALESCE(SUM(CASE WHEN jl.posting_type = 'debit'
                                 THEN jl.amount ELSE -jl.amount END), 0) <> 0
            OR (COUNT(jl.id) = 0 AND COALESCE(t.amount, 0) <> 0)
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
                "net": str(net),
                "line_count": line_count,
                "amount": str(amount),
            },
        )
        for txn_id, txn_type, qbo_id, net, line_count, amount in rows
    ]
