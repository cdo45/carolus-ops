"""Fixture generator round-trips: reportlab PDFs must parse back through
the real pipeline parsers (no DB needed — CI-safe)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from docpipe.pdf import pdf_text
from docpipe.receipts import DeterministicTextExtractor
from docpipe.statements import checksum_problems, parse_statement_text
from tests.fixtures.make_statements import (
    FixtureLine,
    ending_balance,
    image_only_pdf,
    receipt_pdf,
    statement_pdf,
)

LINES = [
    FixtureLine(date(2026, 5, 3), "CHECK 1402 ACME SUPPLY",
                Decimal("750.00"), "debit"),
    FixtureLine(date(2026, 5, 10), "BRANCH DEPOSIT",
                Decimal("1500.00"), "credit"),
]


def test_statement_pdf_round_trips_through_parser(tmp_path: Path) -> None:
    beginning = Decimal("5000.00")
    ending = ending_balance(beginning, LINES)
    path = statement_pdf(
        tmp_path / "statement.pdf", period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31), beginning=beginning, ending=ending,
        lines=LINES, stated_count=len(LINES),
    )

    text = pdf_text(path.read_bytes())
    assert text is not None
    data = parse_statement_text(text)
    assert data.beginning_balance == beginning
    assert data.ending_balance == Decimal("5750.00")
    assert len(data.lines) == 2
    assert checksum_problems(data) == [], "generated statements self-audit clean"


def test_receipt_pdf_round_trips_through_extractor(tmp_path: Path) -> None:
    path = receipt_pdf(tmp_path / "receipt.pdf", merchant="GATE SUPPLY CO",
                       txn_date=date(2026, 5, 12), total=Decimal("89.99"))
    fields = DeterministicTextExtractor().extract(
        path.read_bytes(), pdf_text(path.read_bytes())
    )
    assert fields is not None
    assert fields.amount == Decimal("89.99")
    assert fields.txn_date == date(2026, 5, 12)
    assert fields.vendor == "GATE SUPPLY CO"


def test_image_only_pdf_has_no_text_layer(tmp_path: Path) -> None:
    path = image_only_pdf(tmp_path / "scan.pdf")
    assert pdf_text(path.read_bytes()) is None
