"""Statement parsing, the self-audit checksum, and every terminal exit.

PDFs here are hand-built minimal text-layer files (tiny_pdf) so this
chunk needs no PDF-writing dependency; the gate's fixtures use reportlab.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from docpipe.intake import ingest
from docpipe.pdf import pdf_text
from docpipe.statements import (
    StatementParseError,
    checksum_problems,
    extract_statement,
    parse_statement_text,
)
from docpipe.storage import LocalFSStorage
from tests.conftest import make_client


def tiny_pdf(lines: list[str]) -> bytes:
    """Minimal valid one-page PDF with a real text layer (or none)."""
    parts = ["BT /F1 10 Tf"]
    y = 760
    for line in lines:
        escaped = (line.replace("\\", r"\\").replace("(", r"\(")
                   .replace(")", r"\)"))
        parts.append(f"1 0 0 1 40 {y} Tm ({escaped}) Tj")
        y -= 14
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at))
    return bytes(out)


STATEMENT_LINES = [
    "First Interstate Bank",
    "Statement Period: 2026-05-01 to 2026-05-31",
    "Account Number: XXXX1234",
    "Beginning Balance: $5,000.00",
    "Ending Balance: $5,660.01",
    "Transactions: 3",
    "2026-05-03  CHECK 1402 ACME SUPPLY  -750.00",
    "2026-05-10  BRANCH DEPOSIT  +1,500.00",
    "2026-05-12  CARD PURCHASE HOME DEPOT  -89.99",
]


def statement_doc(
    conn: psycopg.Connection, tmp_path: Path, pdf_bytes: bytes,
    name: str = "may-statement.pdf",
) -> tuple[UUID, LocalFSStorage]:
    client_id = make_client(conn)
    storage = LocalFSStorage(root=tmp_path / "store")
    path = tmp_path / name
    path.write_bytes(pdf_bytes)
    document_id = ingest(conn, client_id, path, "email",
                         storage=storage).document_id
    conn.execute(
        "UPDATE documents SET status = 'classified',"
        " doc_type = 'bank_statement' WHERE id = %s",
        (document_id,),
    )
    conn.commit()
    return document_id, storage


def test_tiny_pdf_has_a_real_text_layer() -> None:
    text = pdf_text(tiny_pdf(STATEMENT_LINES))
    assert text is not None
    assert "Beginning Balance: $5,000.00" in text
    assert pdf_text(tiny_pdf([])) is None, "no text layer -> None"


def test_parse_statement_text() -> None:
    data = parse_statement_text("\n".join(STATEMENT_LINES))
    assert data.period_start == date(2026, 5, 1)
    assert data.period_end == date(2026, 5, 31)
    assert data.beginning_balance == Decimal("5000.00")
    assert data.ending_balance == Decimal("5660.01")
    assert data.stated_txn_count == 3
    assert [(line.direction, line.amount) for line in data.lines] == [
        ("debit", Decimal("750.00")),
        ("credit", Decimal("1500.00")),
        ("debit", Decimal("89.99")),
    ]
    assert data.lines[0].description == "CHECK 1402 ACME SUPPLY"


def test_parse_reports_what_is_missing() -> None:
    with pytest.raises(StatementParseError, match="ending balance"):
        parse_statement_text("Statement Period: 2026-05-01 to 2026-05-31\n"
                             "Beginning Balance: $1.00\n")


def test_checksum_passes_to_the_penny() -> None:
    data = parse_statement_text("\n".join(STATEMENT_LINES))
    assert checksum_problems(data) == []


def test_checksum_catches_one_penny_drift() -> None:
    drifted = [line.replace("$5,660.01", "$5,660.02")
               for line in STATEMENT_LINES]
    problems = checksum_problems(parse_statement_text("\n".join(drifted)))
    assert len(problems) == 1 and "balance mismatch" in problems[0]
    assert "5660.01" in problems[0] and "5660.02" in problems[0]


def test_checksum_catches_count_mismatch() -> None:
    miscounted = [line.replace("Transactions: 3", "Transactions: 4")
                  for line in STATEMENT_LINES]
    problems = checksum_problems(parse_statement_text("\n".join(miscounted)))
    assert len(problems) == 1 and "count mismatch" in problems[0]


def test_extract_validated_path(conn: psycopg.Connection, tmp_path: Path) -> None:
    document_id, storage = statement_doc(conn, tmp_path,
                                         tiny_pdf(STATEMENT_LINES))

    status = extract_statement(conn, document_id, storage=storage)

    assert status == "validated"
    row = conn.execute(
        "SELECT status, period_start, period_end, extracted"
        " FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "validated"
    assert (row[1], row[2]) == (date(2026, 5, 1), date(2026, 5, 31))
    assert row[3]["beginning_balance"] == "5000.00"
    assert len(row[3]["lines"]) == 3
    assert row[3]["lines"][1] == {
        "date": "2026-05-10", "description": "BRANCH DEPOSIT",
        "amount": "1500.00", "direction": "credit",
    }


def test_checksum_failure_escalates_and_nothing_lands(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    corrupted = [line.replace("$5,660.01", "$5,760.01")
                 for line in STATEMENT_LINES]
    document_id, storage = statement_doc(conn, tmp_path, tiny_pdf(corrupted))

    status = extract_statement(conn, document_id, storage=storage)

    assert status == "escalated"
    row = conn.execute(
        "SELECT status, escalation_reason, extracted, period_start"
        " FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row == ("escalated", "checksum_failed", None, None), (
        "nothing from a failed statement enters any downstream column"
    )


def test_image_only_pdf_escalates_needs_ocr(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id, storage = statement_doc(conn, tmp_path, tiny_pdf([]))
    assert extract_statement(conn, document_id, storage=storage) == "escalated"
    reason = conn.execute(
        "SELECT escalation_reason FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert reason == ("needs_ocr",)


def test_non_statement_text_escalates_unparseable(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id, storage = statement_doc(
        conn, tmp_path, tiny_pdf(["meeting notes", "nothing bank-like here"])
    )
    assert extract_statement(conn, document_id, storage=storage) == "escalated"
    reason = conn.execute(
        "SELECT escalation_reason FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert reason == ("unparseable",)


def test_extract_guards_type_and_status(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id, storage = statement_doc(conn, tmp_path,
                                         tiny_pdf(STATEMENT_LINES))
    conn.execute(
        "UPDATE documents SET doc_type = 'receipt' WHERE id = %s",
        (document_id,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="classified bank_statement"):
        extract_statement(conn, document_id, storage=storage)
