"""Generate text-layer fixture PDFs (reportlab) for the Phase 4 gate.

Statements are SYNTHESIZED FROM CANONICAL transactions: the lines come
from journal_lines on the chosen bank account, so a clean statement must
reconcile perfectly against the books it was built from — and the
deliberately broken variants (corrupted balance, phantom line, image-only
page) must fail in exactly the intended way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from docpipe.pdf import pdf_text
from docpipe.statements import checksum_problems, parse_statement_text


@dataclass(frozen=True)
class FixtureLine:
    date: date
    description: str
    amount: Decimal  # positive
    direction: str  # 'credit' (money in) | 'debit' (money out)

    def render(self) -> str:
        sign = "+" if self.direction == "credit" else "-"
        return f"{self.date.isoformat()}  {self.description}  {sign}{self.amount:,.2f}"


_TOP = 750
_BOTTOM = 50
_LEADING = 14


def _write_text_pdf(path: Path, lines: list[str]) -> Path:
    page = canvas.Canvas(str(path), pagesize=letter)

    def fresh_text() -> Any:
        text = page.beginText(40, _TOP)
        text.setFont("Helvetica", 10)
        text.setLeading(_LEADING)
        return text

    text = fresh_text()
    y = _TOP
    for line in lines:
        if y <= _BOTTOM:  # paginate before a line would fall off the page
            page.drawText(text)
            page.showPage()
            text = fresh_text()
            y = _TOP
        text.textLine(line)
        y -= _LEADING
    page.drawText(text)
    page.showPage()
    page.save()
    return path


def statement_lines_from_canonical(
    conn: psycopg.Connection, client_id: UUID, account_id: UUID,
    period_start: date, period_end: date, *, balanced_only: bool = True,
) -> list[FixtureLine]:
    """Bank-account activity from the books, as statement lines.

    balanced_only (default) restricts to transactions whose journal lines net
    to zero AND carry no open transform_warning — so a synthesized "clean"
    statement is built only from books the pipeline itself considers sound,
    never from the unbalanced / account-unresolved rows that would make it
    fail its own checksum.
    """
    rows = conn.execute(
        """
        SELECT t.txn_date, t.txn_type, t.qbo_id, jl.amount, jl.posting_type
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s AND jl.account_id = %s
          AND t.qbo_deleted_at IS NULL
          AND t.txn_date BETWEEN %s AND %s
          AND (NOT %s OR (
              NOT EXISTS (
                  SELECT 1 FROM flags f
                  WHERE f.client_id = t.client_id
                    AND f.rule_code = 'transform_warning' AND f.status = 'open'
                    AND f.source_ref = 'qbo:' || t.txn_type || ':' || t.qbo_id)
              AND (SELECT COALESCE(SUM(CASE WHEN jl2.posting_type = 'debit'
                                            THEN jl2.amount ELSE -jl2.amount END), 0)
                   FROM journal_lines jl2 WHERE jl2.transaction_id = t.id) = 0))
        ORDER BY t.txn_date, t.qbo_id, jl.line_no
        """,
        (client_id, account_id, period_start, period_end, balanced_only),
    ).fetchall()
    return [
        FixtureLine(
            date=txn_date,
            description=f"{txn_type.upper()} {qbo_id}",
            amount=amount,
            # books debit on a bank account = money in = statement credit
            direction="credit" if posting_type == "debit" else "debit",
        )
        for txn_date, txn_type, qbo_id, amount, posting_type in rows
    ]


def ending_balance(beginning: Decimal, lines: list[FixtureLine]) -> Decimal:
    total = beginning
    for line in lines:
        total += line.amount if line.direction == "credit" else -line.amount
    return total


def _money_str(value: Decimal) -> str:
    """Sign BEFORE the dollar: '-$1,234.56', not '$-1,234.56' — the latter is
    what the pipeline parser (docpipe/statements._ENDING/_BEGINNING) rejects,
    turning a negative-balance statement into an 'unparseable' escalation."""
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def statement_pdf(
    path: Path, *, period_start: date, period_end: date, beginning: Decimal,
    ending: Decimal, lines: list[FixtureLine], stated_count: int | None = None,
    bank_name: str = "First Interstate Bank",
) -> Path:
    body = [
        bank_name,
        f"Statement Period: {period_start.isoformat()} to {period_end.isoformat()}",
        "Account Number: XXXX1234",
        f"Beginning Balance: {_money_str(beginning)}",
        f"Ending Balance: {_money_str(ending)}",
    ]
    if stated_count is not None:
        body.append(f"Transactions: {stated_count}")
    body.append("")
    body.extend(line.render() for line in lines)
    return _write_text_pdf(path, body)


def write_validated_statement(path: Path, **kwargs: Any) -> Path:
    """Write a statement that MUST pass the pipeline's own checksum.

    Renders via statement_pdf, then re-reads and runs the SAME parse +
    checksum the pipeline applies; fails loudly here, naming the imbalance,
    if it does not tie — so a "clean" fixture can never reach the rec step
    as a checksum_failed escalation. Use only for statements meant to
    validate (NOT the deliberately-corrupted or image-only ones).
    """
    statement_pdf(path, **kwargs)
    text = pdf_text(path.read_bytes())
    problems = (checksum_problems(parse_statement_text(text)) if text
                else ["no text layer extracted"])
    if problems:
        raise AssertionError(
            f"fixture generator built a non-validating statement {path.name}:"
            f" {'; '.join(problems)}"
        )
    return path


def receipt_pdf(path: Path, *, merchant: str, txn_date: date,
                total: Decimal, footer: str | None = None) -> Path:
    lines = [
        "RECEIPT",
        f"MERCHANT: {merchant}",
        f"DATE: {txn_date.isoformat()}",
        f"Subtotal {total - Decimal('1.00'):,.2f}",
        f"TOTAL: ${total:,.2f}",
        "THANK YOU",
    ]
    if footer:  # e.g. a gate run nonce: changes the bytes, hence the sha256
        lines.append(footer)
    return _write_text_pdf(path, lines)


def image_only_pdf(path: Path, *, salt: int = 0) -> Path:
    """A page with marks but NO text layer — the needs_ocr case.

    salt shifts the geometry so different gate runs produce different
    bytes (and therefore distinct sha256 identities) on purpose.
    """
    offset = salt % 40
    page = canvas.Canvas(str(path), pagesize=letter)
    page.rect(72 + offset, 600, 450, 120, stroke=1, fill=0)
    page.line(72, 580 - offset, 522, 580 - offset)
    page.showPage()
    page.save()
    return path
