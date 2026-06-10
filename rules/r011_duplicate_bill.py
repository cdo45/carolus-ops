"""R011 — same vendor, same document number, different bills.

WHY: vendors re-send invoices; AP enters them again under the same invoice
number. Unlike R010 (amount heuristic), a duplicated vendor doc number is
near-certain double entry — the vendor's own reference appears twice. Also
catches the fraud variant: a re-submitted invoice with the amount nudged.
Every member of the group is flagged (any of them may be the one to void).
Severity critical.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R011"
severity: str = "critical"
title: str = "Duplicate vendor document number"
description: str = (
    "Multiple Bills for the same vendor share a DocNumber. Vendor "
    "references are unique per invoice — duplicates mean double entry "
    "(or a re-submitted invoice)."
)


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        WITH groups AS (
            SELECT entity_id, doc_number,
                   array_agg(qbo_id ORDER BY qbo_id) AS group_qbo_ids
            FROM transactions
            WHERE client_id = %(client_id)s
              AND txn_type = 'Bill'
              AND entity_id IS NOT NULL
              AND doc_number IS NOT NULL AND btrim(doc_number) <> ''
              AND txn_date <= %(as_of)s
              AND qbo_deleted_at IS NULL
            GROUP BY entity_id, doc_number
            HAVING count(*) > 1
        )
        SELECT t.id, t.qbo_id, t.doc_number, e.name, g.group_qbo_ids, t.amount
        FROM transactions t
        JOIN groups g ON g.entity_id = t.entity_id
                     AND g.doc_number = t.doc_number
        JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %(client_id)s
          AND t.txn_type = 'Bill'
          AND t.txn_date <= %(as_of)s
          AND t.qbo_deleted_at IS NULL
        """,
        {"client_id": client_id, "as_of": as_of},
    ).fetchall()
    return [
        Finding(
            source_type="transaction",
            source_ref=str(txn_id),
            detail={
                "qbo_id": qbo_id,
                "doc_number": doc_number,
                "vendor": vendor,
                "group_qbo_ids": group_qbo_ids,
                "amount": str(amount),
            },
        )
        for txn_id, qbo_id, doc_number, vendor, group_qbo_ids, amount in rows
    ]
