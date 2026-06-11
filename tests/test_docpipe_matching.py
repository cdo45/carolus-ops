"""Receipt extraction seam, the three matching exits, vendor narrowing,
and the request list."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from docpipe.matching import match_document
from docpipe.receipts import (
    DeterministicTextExtractor,
    extract_receipt,
)
from docpipe.requests import format_request_list, generate_request_list
from docpipe.storage import LocalFSStorage
from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account, make_entity
from tests.test_docpipe_statements import tiny_pdf

PERIOD = (date(2026, 5, 1), date(2026, 5, 31))


def receipt_doc(
    conn: psycopg.Connection, client_id: UUID, *, amount: str,
    txn_date: str = "2026-05-12", vendor: str | None = "HOME DEPOT",
    status: str = "validated", suffix: str = "r1",
) -> UUID:
    extracted: dict[str, Any] | None = {
        "amount": amount, "txn_date": txn_date, "vendor": vendor,
    }
    if status == "classified":
        extracted = None
    row = conn.execute(
        """
        INSERT INTO documents (client_id, storage_ref, sha256, status,
                               doc_type, extracted, filename)
        VALUES (%s, %s, %s, %s, 'receipt', %s, %s)
        RETURNING id
        """,
        (client_id, "k" + suffix, "sha-" + suffix, status,
         Jsonb(extracted) if extracted else None, f"receipt-{suffix}.pdf"),
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def seed_spend(conn: psycopg.Connection, client_id: UUID) -> dict[str, UUID]:
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    cogs = make_account(conn, client_id, name="Job Materials",
                        acct_type="Cost of Goods Sold")
    home_depot = make_entity(conn, client_id, kind="vendor", name="Home Depot")
    ferguson = make_entity(conn, client_id, kind="vendor", name="Ferguson")
    txn = balanced_purchase(conn, client_id, amount="89.99",
                            txn_date=date(2026, 5, 12), entity_id=home_depot,
                            bank=bank, expense=cogs)
    conn.commit()
    return {"bank": bank, "cogs": cogs, "home_depot": home_depot,
            "ferguson": ferguson, "txn": txn}


# ---------------------------------------------------------- receipt seam


def test_deterministic_extractor_parses_fixture_grammar(
    conn: psycopg.Connection, tmp_path: object
) -> None:
    client_id = make_client(conn)
    storage = LocalFSStorage(root=tmp_path / "store")  # type: ignore[operator]
    pdf = tiny_pdf(["RECEIPT", "MERCHANT: HOME DEPOT", "DATE: 2026-05-12",
                    "TOTAL: $89.99"])
    storage.put("a" * 64, pdf)
    document_id = receipt_doc(conn, client_id, amount="0", status="classified")
    conn.execute("UPDATE documents SET storage_ref = %s WHERE id = %s",
                 ("a" * 64, document_id))
    conn.commit()

    status = extract_receipt(conn, document_id,
                             extractor=DeterministicTextExtractor(),
                             storage=storage)

    assert status == "validated"
    extracted = conn.execute(
        "SELECT extracted FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert extracted == ({"amount": "89.99", "txn_date": "2026-05-12",
                          "vendor": "HOME DEPOT"},)


def test_stub_extractor_escalates_extraction_failed(
    conn: psycopg.Connection, tmp_path: object
) -> None:
    client_id = make_client(conn)
    storage = LocalFSStorage(root=tmp_path / "store")  # type: ignore[operator]
    storage.put("b" * 64, b"photo bytes, no text layer")
    document_id = receipt_doc(conn, client_id, amount="0", status="classified",
                              suffix="r2")
    conn.execute("UPDATE documents SET storage_ref = %s WHERE id = %s",
                 ("b" * 64, document_id))
    conn.commit()

    status = extract_receipt(conn, document_id, storage=storage)  # stub

    assert status == "escalated"
    row = conn.execute(
        "SELECT escalation_reason FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row == ("extraction_failed",)


# ---------------------------------------------------------- matching exits


def test_single_candidate_matches_and_backs_txn(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    books = seed_spend(conn, client_id)
    document_id = receipt_doc(conn, client_id, amount="89.99")

    assert match_document(conn, document_id) == "matched"

    doc = conn.execute(
        "SELECT status, matched_txn FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert doc == ("matched", books["txn"])
    backed = conn.execute(
        "SELECT doc_status FROM transactions WHERE id = %s", (books["txn"],)
    ).fetchone()
    assert backed == ("backed",)


def test_multiple_candidates_escalate_with_list(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    books = seed_spend(conn, client_id)
    balanced_purchase(conn, client_id, amount="89.99",
                      txn_date=date(2026, 5, 14), entity_id=books["home_depot"],
                      bank=books["bank"], expense=books["cogs"])
    conn.commit()
    document_id = receipt_doc(conn, client_id, amount="89.99")

    assert match_document(conn, document_id) == "escalated"

    row = conn.execute(
        "SELECT escalation_reason, extracted -> 'match_candidates'"
        " FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "ambiguous_match"
    assert len(row[1]) == 2, "escalation lists every candidate"
    assert {c["txn_date"] for c in row[1]} == {"2026-05-12", "2026-05-14"}


def test_zero_candidates_escalate_orphan(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    seed_spend(conn, client_id)
    document_id = receipt_doc(conn, client_id, amount="123.45")

    assert match_document(conn, document_id) == "escalated"
    row = conn.execute(
        "SELECT escalation_reason FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row == ("no_matching_txn",)


def test_vendor_trigram_narrows_ambiguity(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_spend(conn, client_id)
    # same amount, same window, DIFFERENT vendor — name narrowing resolves it
    balanced_purchase(conn, client_id, amount="89.99",
                      txn_date=date(2026, 5, 13), entity_id=books["ferguson"],
                      bank=books["bank"], expense=books["cogs"])
    conn.commit()
    document_id = receipt_doc(conn, client_id, amount="89.99",
                              vendor="Home Depot #4821")

    assert match_document(conn, document_id) == "matched"
    matched = conn.execute(
        "SELECT matched_txn FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert matched == (books["txn"],), "trigram picked the Home Depot txn"


def test_matching_guards_status(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    document_id = receipt_doc(conn, client_id, amount="1.00",
                              status="classified", suffix="r9")
    with pytest.raises(ValueError, match="validated receipt/invoice"):
        match_document(conn, document_id)


# ---------------------------------------------------------- request list


def test_request_list_contents(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    books = seed_spend(conn, client_id)  # $89.99 unbacked purchase in period
    # backed txn: excluded
    backed = balanced_purchase(conn, client_id, amount="200.00",
                               txn_date=date(2026, 5, 5),
                               entity_id=books["home_depot"],
                               bank=books["bank"], expense=books["cogs"])
    conn.execute("UPDATE transactions SET doc_status = 'backed' WHERE id = %s",
                 (backed,))
    # under the $75 floor: excluded
    balanced_purchase(conn, client_id, amount="40.00",
                      txn_date=date(2026, 5, 6), entity_id=books["home_depot"],
                      bank=books["bank"], expense=books["cogs"])
    # orphan receipt: included in unmatched documents
    orphan = receipt_doc(conn, client_id, amount="123.45", suffix="r3")
    match_document(conn, orphan)
    conn.commit()

    requests = generate_request_list(conn, client_id, *PERIOD)

    assert [t["amount"] for t in requests.undocumented_txns] == ["89.99"]
    assert requests.undocumented_txns[0]["vendor"] == "Home Depot"
    assert requests.undocumented_txns[0]["accounts"] == ["Job Materials"]
    assert [d["reason"] for d in requests.unmatched_documents] == [
        "no_matching_txn",
    ]

    rendered = format_request_list("Fixture Co", requests)
    assert "Missing documentation (1):" in rendered
    assert "$89.99  Home Depot" in rendered
    assert "receipt-r3.pdf" in rendered
