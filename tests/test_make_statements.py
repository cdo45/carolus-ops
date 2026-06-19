"""Fixture generator: round-trips through the real pipeline parsers, plus
the clean-statement invariants — the balanced-only filter and the
checksum guard that keep a synthesized clean statement from ever reaching
the rec step as an escalation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from docpipe.pdf import pdf_text
from docpipe.receipts import DeterministicTextExtractor
from docpipe.statements import checksum_problems, parse_statement_text
from tests.conftest import make_client
from tests.factories import make_account, make_txn
from tests.fixtures.make_statements import (
    FixtureLine,
    ending_balance,
    image_only_pdf,
    receipt_pdf,
    statement_lines_from_canonical,
    statement_pdf,
    write_validated_statement,
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


def test_write_validated_statement_fails_loudly(tmp_path: Path) -> None:
    """A tying statement writes; a non-tying one fails in the generator,
    naming the imbalance — never reaching the pipeline as an escalation."""
    beginning = Decimal("5000.00")
    ending = ending_balance(beginning, LINES)
    common = dict(period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                  beginning=beginning, lines=LINES, stated_count=len(LINES))
    write_validated_statement(tmp_path / "ok.pdf", ending=ending, **common)

    with pytest.raises(AssertionError, match="non-validating statement"):
        write_validated_statement(tmp_path / "bad.pdf",
                                  ending=ending + Decimal("0.01"), **common)


def test_balanced_only_excludes_unbalanced_and_warned(
    conn: psycopg.Connection,
) -> None:
    """Clean-statement lines come only from balanced, warning-free books —
    the unbalanced / account-unresolved rows that would break the checksum
    are excluded (the live gate_phase4 crash)."""
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    income = make_account(conn, client_id, name="Income", acct_type="Income")
    make_txn(conn, client_id, txn_type="Deposit", txn_date=date(2026, 5, 3),
             qbo_id="D1", amount="100.00",
             lines=[{"account": bank, "amount": "100.00", "posting": "debit"},
                    {"account": income, "amount": "100.00", "posting": "credit"}])
    # unbalanced (bank line only) and carrying an open transform_warning
    make_txn(conn, client_id, txn_type="Purchase", txn_date=date(2026, 5, 4),
             qbo_id="P9", amount="50.00",
             lines=[{"account": bank, "amount": "50.00", "posting": "credit"}])
    conn.execute(
        "INSERT INTO flags (client_id, rule_code, severity, status, source_type,"
        " source_ref, detail) VALUES (%s, 'transform_warning', 'warn', 'open',"
        " 'transaction', 'qbo:Purchase:P9', 'unbalanced lines')",
        (client_id,),
    )
    conn.commit()

    window = (conn, client_id, bank, date(2026, 5, 1), date(2026, 5, 31))
    everything = statement_lines_from_canonical(*window, balanced_only=False)
    clean = statement_lines_from_canonical(*window, balanced_only=True)
    assert {line.description for line in everything} == {"DEPOSIT D1", "PURCHASE P9"}
    assert {line.description for line in clean} == {"DEPOSIT D1"}, (
        "the unbalanced, warned transaction is excluded from clean statements"
    )
