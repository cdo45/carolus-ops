"""Bank-statement extraction with a self-audit checksum.

THE CHECKSUM is the heart of Phase 4: beginning + credits - debits must
equal the stated ending balance TO THE PENNY, and the parsed line count
must match any stated transaction count. A statement that passes is
arithmetically self-consistent — we trust our own parse of it. One that
fails escalates with reason 'checksum_failed' and NOTHING from it enters
any downstream table: better no data than silently wrong data.

Other terminal exits: image-only PDFs escalate 'needs_ocr' (the vision
extractor is a later, stubbed interface — never silently parsed);
text that doesn't match the statement grammar escalates 'unparseable'.

Statement grammar (the fixture generator emits exactly this shape):

    Statement Period: 2026-05-01 to 2026-05-31
    Beginning Balance: $5,000.00
    Ending Balance: $4,210.01
    Transactions: 3                      (optional)
    2026-05-03  CHECK 1402 ACME SUPPLY   -750.00
    2026-05-10  BRANCH DEPOSIT           +1,500.00

Line sign convention: negative = debit (money out), else credit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from docpipe.pdf import pdf_text
from docpipe.storage import DocumentStorage, LocalFSStorage

_PERIOD = re.compile(
    r"statement period:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_BEGINNING = re.compile(r"beginning balance:\s*(-?\$?[\d,]+\.\d{2})", re.IGNORECASE)
_ENDING = re.compile(r"ending balance:\s*(-?\$?[\d,]+\.\d{2})", re.IGNORECASE)
_COUNT = re.compile(r"transactions:\s*(\d+)", re.IGNORECASE)
_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<description>.+?)\s+"
    r"(?P<amount>[+-]?\$?[\d,]+\.\d{2})$"
)


class StatementParseError(Exception):
    """Text exists but does not follow the statement grammar."""


@dataclass(frozen=True)
class StatementLine:
    date: date
    description: str
    amount: Decimal  # always positive
    direction: str  # 'credit' (in) | 'debit' (out)


@dataclass(frozen=True)
class StatementData:
    period_start: date
    period_end: date
    beginning_balance: Decimal
    ending_balance: Decimal
    lines: list[StatementLine]
    stated_txn_count: int | None

    def as_jsonb(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "beginning_balance": str(self.beginning_balance),
            "ending_balance": str(self.ending_balance),
            "stated_txn_count": self.stated_txn_count,
            "lines": [
                {
                    "date": line.date.isoformat(),
                    "description": line.description,
                    "amount": str(line.amount),
                    "direction": line.direction,
                }
                for line in self.lines
            ],
        }


def _money(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace("$", "").replace(",", "").replace("+", ""))
    except InvalidOperation as exc:
        raise StatementParseError(f"unparseable amount {raw!r}") from exc


def parse_statement_text(text: str) -> StatementData:
    period = _PERIOD.search(text)
    beginning = _BEGINNING.search(text)
    ending = _ENDING.search(text)
    if not (period and beginning and ending):
        missing = [
            name
            for name, found in (("period", period), ("beginning balance",
                                beginning), ("ending balance", ending))
            if not found
        ]
        raise StatementParseError(f"missing: {', '.join(missing)}")
    count = _COUNT.search(text)

    lines: list[StatementLine] = []
    for raw_line in text.splitlines():
        match = _LINE.match(raw_line.strip())
        if match is None:
            continue
        amount = _money(match["amount"])
        lines.append(StatementLine(
            date=date.fromisoformat(match["date"]),
            description=match["description"].strip(),
            amount=abs(amount),
            direction="debit" if amount < 0 else "credit",
        ))
    return StatementData(
        period_start=date.fromisoformat(period[1]),
        period_end=date.fromisoformat(period[2]),
        beginning_balance=_money(beginning[1]),
        ending_balance=_money(ending[1]),
        lines=lines,
        stated_txn_count=int(count[1]) if count else None,
    )


def checksum_problems(data: StatementData) -> list[str]:
    """Empty list = the statement is arithmetically self-consistent."""
    problems: list[str] = []
    credits = sum(
        (line.amount for line in data.lines if line.direction == "credit"),
        Decimal("0.00"),
    )
    debits = sum(
        (line.amount for line in data.lines if line.direction == "debit"),
        Decimal("0.00"),
    )
    computed = data.beginning_balance + credits - debits
    if computed != data.ending_balance:
        problems.append(
            f"balance mismatch: beginning {data.beginning_balance}"
            f" + credits {credits} - debits {debits} = {computed},"
            f" statement says {data.ending_balance}"
        )
    if data.stated_txn_count is not None and data.stated_txn_count != len(data.lines):
        problems.append(
            f"count mismatch: parsed {len(data.lines)} lines,"
            f" statement says {data.stated_txn_count}"
        )
    return problems


def _escalate(conn: psycopg.Connection, document_id: UUID, reason: str) -> str:
    conn.execute(
        "UPDATE documents SET status = 'escalated', escalation_reason = %s"
        " WHERE id = %s",
        (reason, document_id),
    )
    conn.commit()
    return "escalated"


def extract_statement(
    conn: psycopg.Connection,
    document_id: UUID,
    *,
    storage: DocumentStorage | None = None,
) -> str:
    """Extract + checksum one classified bank statement; returns status."""
    storage = storage or LocalFSStorage()
    row = conn.execute(
        "SELECT storage_ref, doc_type, status FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"document {document_id} does not exist")
    storage_ref, doc_type, status = row
    if doc_type != "bank_statement" or status != "classified":
        raise ValueError(
            f"document {document_id} is ({doc_type!r}, {status!r}) —"
            " extraction needs a classified bank_statement"
        )

    text = pdf_text(storage.get(storage_ref))
    if text is None:
        return _escalate(conn, document_id, "needs_ocr")

    try:
        data = parse_statement_text(text)
    except StatementParseError:
        return _escalate(conn, document_id, "unparseable")

    if checksum_problems(data):
        # NOTHING from this document enters any downstream table
        return _escalate(conn, document_id, "checksum_failed")

    conn.execute(
        """
        UPDATE documents
        SET status = 'validated', extracted = %s,
            period_start = %s, period_end = %s
        WHERE id = %s
        """,
        (Jsonb(data.as_jsonb()), data.period_start, data.period_end,
         document_id),
    )
    conn.commit()
    return "validated"
