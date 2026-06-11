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
from uuid import UUID

import psycopg
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class FixtureLine:
    date: date
    description: str
    amount: Decimal  # positive
    direction: str  # 'credit' (money in) | 'debit' (money out)

    def render(self) -> str:
        sign = "+" if self.direction == "credit" else "-"
        return f"{self.date.isoformat()}  {self.description}  {sign}{self.amount:,.2f}"


def _write_text_pdf(path: Path, lines: list[str]) -> Path:
    page = canvas.Canvas(str(path), pagesize=letter)
    text = page.beginText(40, 750)
    text.setFont("Helvetica", 10)
    for line in lines:
        text.textLine(line)
    page.drawText(text)
    page.showPage()
    page.save()
    return path


def statement_lines_from_canonical(
    conn: psycopg.Connection, client_id: UUID, account_id: UUID,
    period_start: date, period_end: date,
) -> list[FixtureLine]:
    """Bank-account activity from the books, as statement lines."""
    rows = conn.execute(
        """
        SELECT t.txn_date, t.txn_type, t.qbo_id, jl.amount, jl.posting_type
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s AND jl.account_id = %s
          AND t.qbo_deleted_at IS NULL
          AND t.txn_date BETWEEN %s AND %s
        ORDER BY t.txn_date, t.qbo_id, jl.line_no
        """,
        (client_id, account_id, period_start, period_end),
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


def statement_pdf(
    path: Path, *, period_start: date, period_end: date, beginning: Decimal,
    ending: Decimal, lines: list[FixtureLine], stated_count: int | None = None,
    bank_name: str = "First Interstate Bank",
) -> Path:
    body = [
        bank_name,
        f"Statement Period: {period_start.isoformat()} to {period_end.isoformat()}",
        "Account Number: XXXX1234",
        f"Beginning Balance: ${beginning:,.2f}",
        f"Ending Balance: ${ending:,.2f}",
    ]
    if stated_count is not None:
        body.append(f"Transactions: {stated_count}")
    body.append("")
    body.extend(line.render() for line in lines)
    return _write_text_pdf(path, body)


def receipt_pdf(path: Path, *, merchant: str, txn_date: date,
                total: Decimal) -> Path:
    return _write_text_pdf(path, [
        "RECEIPT",
        f"MERCHANT: {merchant}",
        f"DATE: {txn_date.isoformat()}",
        f"Subtotal {total - Decimal('1.00'):,.2f}",
        f"TOTAL: ${total:,.2f}",
        "THANK YOU",
    ])


def image_only_pdf(path: Path) -> Path:
    """A page with marks but NO text layer — the needs_ocr case."""
    page = canvas.Canvas(str(path), pagesize=letter)
    page.rect(72, 600, 450, 120, stroke=1, fill=0)
    page.line(72, 580, 522, 580)
    page.showPage()
    page.save()
    return path
