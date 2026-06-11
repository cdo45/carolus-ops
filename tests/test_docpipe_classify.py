"""Classification: heuristics per type, and all three exits
(deterministic / LLM-interface canned / escalated)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from docpipe.classify import (
    ClassificationResult,
    classify_document,
    heuristic_classification,
)
from docpipe.intake import ingest
from docpipe.storage import LocalFSStorage
from tests.conftest import make_client

STATEMENT_TEXT = """First Interstate Bank
Statement Period: 2026-05-01 to 2026-05-31
Account Number: XXXX1234
Beginning Balance: $5,000.00
Ending Balance: $4,200.00
"""
RECEIPT_TEXT = """HOME DEPOT #4821
Subtotal 82.41
Total 89.99
Cash Tendered 100.00
Change Due 10.01
"""
INVOICE_TEXT = """INVOICE
Invoice Number: 2210
Bill To: Gate Three Constructors
Due Date: 2026-06-15
"""
AMBIGUOUS_TEXT = "Thanks for the quick turnaround on the job last week."


def make_doc(conn: psycopg.Connection, tmp_path: Path, name: str) -> UUID:
    client_id = make_client(conn)
    path = tmp_path / name
    path.write_bytes(f"bytes of {name}".encode())
    storage = LocalFSStorage(root=tmp_path / "store")
    return ingest(conn, client_id, path, "email", storage=storage).document_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (STATEMENT_TEXT, "bank_statement"),
        (RECEIPT_TEXT, "receipt"),
        (INVOICE_TEXT, "invoice"),
    ],
)
def test_heuristics_recognize_each_type(text: str, expected: str) -> None:
    result = heuristic_classification("scan-001.pdf", "application/pdf", text)
    assert result.confident is True
    assert result.doc_type == expected
    assert result.signals, "confident classification names its signals"


def test_heuristics_use_filename_when_text_is_thin() -> None:
    result = heuristic_classification(
        "may-statement.pdf", "application/pdf", "Beginning Balance: $1.00"
    )
    assert result == ClassificationResult(
        doc_type="bank_statement", confident=True,
        signals=["filename:statement", "text:beginning balance"],
    )


def test_heuristics_abstain_on_ambiguity() -> None:
    result = heuristic_classification("scan-001.pdf", None, AMBIGUOUS_TEXT)
    assert result.confident is False and result.doc_type is None


def test_no_text_layer_classifies_on_unambiguous_filename_alone() -> None:
    """Scans have no text — a clear filename must still classify so the
    document reaches extraction (where needs_ocr escalates precisely)."""
    result = heuristic_classification("may-statement.pdf", "application/pdf",
                                      None)
    assert result.confident is True and result.doc_type == "bank_statement"
    # but no text AND no filename signal stays unclassifiable
    nothing = heuristic_classification("scan-0042.pdf", "application/pdf", None)
    assert nothing.confident is False and nothing.doc_type is None


def test_exit_one_deterministic(conn: psycopg.Connection, tmp_path: Path) -> None:
    document_id = make_doc(conn, tmp_path, "may-2026-statement.pdf")

    status = classify_document(conn, document_id, text=STATEMENT_TEXT)

    assert status == "classified"
    row = conn.execute(
        "SELECT doc_type, status, escalation_reason FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row == ("bank_statement", "classified", None)


def test_exit_two_llm_interface_with_canned_answer(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id = make_doc(conn, tmp_path, "scan-0042.pdf")

    class CannedClassifier:
        calls: list[tuple[str | None, str | None]] = []

        def classify(self, filename: str | None, text: str | None) -> str | None:
            self.calls.append((filename, text))
            return "contract"

    canned = CannedClassifier()
    status = classify_document(conn, document_id, text=AMBIGUOUS_TEXT, llm=canned)

    assert status == "classified"
    assert canned.calls == [("scan-0042.pdf", AMBIGUOUS_TEXT)], (
        "LLM seam consulted exactly once, after heuristics abstained"
    )
    row = conn.execute(
        "SELECT doc_type, status FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert row == ("contract", "classified")


def test_exit_three_escalates_unclassifiable(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id = make_doc(conn, tmp_path, "scan-0099.pdf")

    status = classify_document(conn, document_id, text=AMBIGUOUS_TEXT)  # stub LLM

    assert status == "escalated"
    row = conn.execute(
        "SELECT doc_type, status, escalation_reason FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row == (None, "escalated", "unclassifiable")


def test_classifier_returning_garbage_is_rejected(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id = make_doc(conn, tmp_path, "scan-0100.pdf")

    class GarbageClassifier:
        def classify(self, filename: str | None, text: str | None) -> str | None:
            return "tax_form"  # not in DOC_TYPES

    with pytest.raises(ValueError, match="unknown type"):
        classify_document(conn, document_id, text=AMBIGUOUS_TEXT,
                          llm=GarbageClassifier())


def test_only_received_documents_classify(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    document_id = make_doc(conn, tmp_path, "statement-x.pdf")
    classify_document(conn, document_id, text=STATEMENT_TEXT)
    with pytest.raises(ValueError, match="not 'received'"):
        classify_document(conn, document_id, text=STATEMENT_TEXT)
