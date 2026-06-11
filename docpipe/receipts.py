"""Receipt/invoice field extraction behind a model-judgment seam.

Real-world receipts are photos and scans — extracting (amount, date,
vendor) from those is vision-model work that does NOT exist in this
phase. The seam is ReceiptExtractor:

  - StubReceiptExtractor: always abstains -> document escalates with
    reason 'extraction_failed' (never silently guessed).
  - DeterministicTextExtractor: parses the fixture grammar used by tests
    and the gate (MERCHANT:/DATE:/TOTAL: lines in the text layer) —
    deterministic code, not a model; it exists so the pipeline can be
    proven end-to-end without any live model call.

extract_receipt() moves a classified receipt/invoice to 'validated' with
extracted = {amount, txn_date, vendor} or escalates. Matching consumes
only validated documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from docpipe.pdf import pdf_text
from docpipe.storage import DocumentStorage, LocalFSStorage


@dataclass(frozen=True)
class ReceiptData:
    amount: Decimal
    txn_date: date
    vendor: str | None


class ReceiptExtractor(Protocol):
    """Model seam: bytes + optional text layer -> fields, or None."""

    def extract(self, data: bytes, text: str | None) -> ReceiptData | None: ...


class StubReceiptExtractor:
    """No vision model is wired in this phase — always abstains."""

    def extract(self, data: bytes, text: str | None) -> ReceiptData | None:
        return None


_MERCHANT = re.compile(r"merchant:\s*(.+)", re.IGNORECASE)
_DATE = re.compile(r"date:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_TOTAL = re.compile(r"total:\s*\$?(-?[\d,]+\.\d{2})", re.IGNORECASE)


class DeterministicTextExtractor:
    """Parses the fixture receipt grammar from the PDF text layer."""

    def extract(self, data: bytes, text: str | None) -> ReceiptData | None:
        if text is None:
            return None
        merchant = _MERCHANT.search(text)
        date_match = _DATE.search(text)
        total = _TOTAL.search(text)
        if not (date_match and total):
            return None
        try:
            amount = Decimal(total[1].replace(",", ""))
        except InvalidOperation:
            return None
        return ReceiptData(
            amount=amount,
            txn_date=date.fromisoformat(date_match[1]),
            vendor=merchant[1].strip() if merchant else None,
        )


def extract_receipt(
    conn: psycopg.Connection,
    document_id: UUID,
    *,
    extractor: ReceiptExtractor | None = None,
    storage: DocumentStorage | None = None,
) -> str:
    """Extract fields from one classified receipt/invoice; returns status."""
    storage = storage or LocalFSStorage()
    row = conn.execute(
        "SELECT storage_ref, doc_type, status FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"document {document_id} does not exist")
    storage_ref, doc_type, status = row
    if doc_type not in ("receipt", "invoice") or status != "classified":
        raise ValueError(
            f"document {document_id} is ({doc_type!r}, {status!r}) —"
            " receipt extraction needs a classified receipt/invoice"
        )

    data = storage.get(storage_ref)
    fields = (extractor or StubReceiptExtractor()).extract(data, pdf_text(data))
    if fields is None:
        conn.execute(
            "UPDATE documents SET status = 'escalated',"
            " escalation_reason = 'extraction_failed' WHERE id = %s",
            (document_id,),
        )
        conn.commit()
        return "escalated"

    conn.execute(
        """
        UPDATE documents SET status = 'validated', extracted = %s
        WHERE id = %s
        """,
        (Jsonb({
            "amount": str(fields.amount),
            "txn_date": fields.txn_date.isoformat(),
            "vendor": fields.vendor,
        }), document_id),
    )
    conn.commit()
    return "validated"
