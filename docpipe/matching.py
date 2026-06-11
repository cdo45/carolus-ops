"""Match validated receipts/invoices to canonical transactions.

Candidate = same client, exact amount, txn_date within ±5 days, txn_type
appropriate to the document (receipt -> money-out types, invoice ->
Invoice/Bill). When the document names a vendor, trigram similarity
against entity names narrows the field (>= 0.3).

Exactly one candidate -> documents.matched_txn set, status='matched',
and the transaction's doc_status flips to 'backed' (the only code path
that does so besides Carlos). Multiple -> escalate 'ambiguous_match'
with the candidates listed in extracted.match_candidates. Zero ->
escalate 'no_matching_txn'. The machine never guesses a match.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

MATCH_WINDOW_DAYS: int = 5
VENDOR_SIMILARITY: float = 0.3

_CANDIDATE_TYPES: dict[str, tuple[str, ...]] = {
    "receipt": ("Purchase", "Bill", "BillPayment"),
    "invoice": ("Invoice", "Bill"),
}


def _candidates(
    conn: psycopg.Connection, client_id: UUID, doc_type: str,
    amount: Decimal, txn_date: date, vendor: str | None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.id, t.txn_type, t.qbo_id, t.txn_date, t.amount,
               COALESCE(e.name, '') AS entity_name,
               CASE WHEN %(vendor)s::text IS NULL OR e.name IS NULL THEN 0
                    ELSE similarity(e.name, %(vendor)s) END AS vendor_sim
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %(client_id)s
          AND t.txn_type = ANY(%(types)s)
          AND t.amount = %(amount)s
          AND t.txn_date BETWEEN %(lo)s AND %(hi)s
          AND t.qbo_deleted_at IS NULL
        ORDER BY t.txn_date, t.qbo_id
        """,
        {
            "client_id": client_id,
            "types": list(_CANDIDATE_TYPES[doc_type]),
            "amount": amount,
            "lo": txn_date - timedelta(days=MATCH_WINDOW_DAYS),
            "hi": txn_date + timedelta(days=MATCH_WINDOW_DAYS),
            "vendor": vendor,
        },
    ).fetchall()
    candidates = [
        {"id": row[0], "txn_type": row[1], "qbo_id": row[2],
         "txn_date": row[3].isoformat(), "amount": str(row[4]),
         "entity_name": row[5], "vendor_sim": float(row[6])}
        for row in rows
    ]
    if vendor:
        named = [c for c in candidates if c["vendor_sim"] >= VENDOR_SIMILARITY]
        if named:
            return named
    return candidates


def match_document(conn: psycopg.Connection, document_id: UUID) -> str:
    """Resolve one validated receipt/invoice; returns the resulting status."""
    row = conn.execute(
        "SELECT client_id, doc_type, status, extracted FROM documents"
        " WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"document {document_id} does not exist")
    client_id, doc_type, status, extracted = row
    if (status != "validated" or doc_type not in _CANDIDATE_TYPES
            or not extracted or "amount" not in extracted):
        raise ValueError(
            f"document {document_id} is ({doc_type!r}, {status!r}) — matching"
            " needs a validated receipt/invoice with extracted fields"
        )

    candidates = _candidates(
        conn, client_id, doc_type,
        Decimal(extracted["amount"]),
        date.fromisoformat(extracted["txn_date"]),
        extracted.get("vendor"),
    )

    if len(candidates) == 1:
        txn_id = candidates[0]["id"]
        conn.execute(
            "UPDATE documents SET status = 'matched', matched_txn = %s"
            " WHERE id = %s",
            (txn_id, document_id),
        )
        conn.execute(
            "UPDATE transactions SET doc_status = 'backed' WHERE id = %s",
            (txn_id,),
        )
        conn.commit()
        return "matched"

    if candidates:
        listed = [
            {key: candidate[key]
             for key in ("txn_type", "qbo_id", "txn_date", "amount",
                         "entity_name")}
            for candidate in candidates
        ]
        conn.execute(
            """
            UPDATE documents
            SET status = 'escalated', escalation_reason = 'ambiguous_match',
                extracted = extracted || %s
            WHERE id = %s
            """,
            (Jsonb({"match_candidates": listed}), document_id),
        )
        conn.commit()
        return "escalated"

    conn.execute(
        "UPDATE documents SET status = 'escalated',"
        " escalation_reason = 'no_matching_txn' WHERE id = %s",
        (document_id,),
    )
    conn.commit()
    return "escalated"
