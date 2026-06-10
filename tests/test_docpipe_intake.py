"""Storage backends and idempotent intake."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from docpipe.intake import ingest
from docpipe.storage import LocalFSStorage, NotConfigured, R2Storage
from tests.conftest import make_client


def test_localfs_round_trip(tmp_path: Path) -> None:
    storage = LocalFSStorage(root=tmp_path)
    key = "ab" + "0" * 62
    assert storage.exists(key) is False
    storage.put(key, b"pdf bytes")
    assert storage.exists(key) is True
    assert storage.get(key) == b"pdf bytes"
    assert (tmp_path / "ab" / key).exists(), "sharded by first two hex chars"


def test_localfs_rejects_non_hash_keys(tmp_path: Path) -> None:
    storage = LocalFSStorage(root=tmp_path)
    with pytest.raises(ValueError, match="sha256"):
        storage.put("../escape", b"x")


def test_r2_stub_raises_not_configured() -> None:
    backend = R2Storage()
    with pytest.raises(NotConfigured):
        backend.put("ab" + "0" * 62, b"x")
    with pytest.raises(NotConfigured):
        backend.get("ab" + "0" * 62)
    with pytest.raises(NotConfigured):
        backend.exists("ab" + "0" * 62)


def test_ingest_creates_row_and_blob(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    client_id = make_client(conn)
    storage = LocalFSStorage(root=tmp_path / "store")
    file_path = tmp_path / "statement-may.pdf"
    file_path.write_bytes(b"%PDF-1.4 fake statement bytes")

    result = ingest(conn, client_id, file_path, "email", storage=storage)

    assert result.created is True
    assert storage.get(result.sha256) == file_path.read_bytes()
    row = conn.execute(
        """
        SELECT status, doc_type, filename, content_type, source_channel,
               storage_ref
        FROM documents WHERE id = %s
        """,
        (result.document_id,),
    ).fetchone()
    assert row == ("received", None, "statement-may.pdf", "application/pdf",
                   "email", result.sha256)


def test_reingest_same_bytes_is_idempotent(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    client_id = make_client(conn)
    storage = LocalFSStorage(root=tmp_path / "store")
    file_path = tmp_path / "receipt.pdf"
    file_path.write_bytes(b"receipt bytes")

    first = ingest(conn, client_id, file_path, "email", storage=storage)
    renamed = tmp_path / "receipt-copy.pdf"  # same bytes, different name
    renamed.write_bytes(b"receipt bytes")
    second = ingest(conn, client_id, renamed, "portal", storage=storage)

    assert second.created is False
    assert second.document_id == first.document_id
    count = conn.execute(
        "SELECT count(*) FROM documents WHERE client_id = %s", (client_id,)
    ).fetchone()
    assert count == (1,), "duplicate upload = same document, zero new rows"


def test_same_bytes_different_clients_are_separate_documents(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    client_a = make_client(conn, realm="realm-a")
    client_b = make_client(conn, realm="realm-b")
    storage = LocalFSStorage(root=tmp_path / "store")
    file_path = tmp_path / "shared.pdf"
    file_path.write_bytes(b"identical bytes")

    a = ingest(conn, client_a, file_path, "email", storage=storage)
    b = ingest(conn, client_b, file_path, "email", storage=storage)

    assert a.created and b.created
    assert a.document_id != b.document_id
    assert a.sha256 == b.sha256, "same blob, one stored copy"
