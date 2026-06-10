"""Document intake: hash, store, record. Idempotent by content.

ingest() is safe to call repeatedly with the same file: the sha256 is
the identity (unique per client), so a duplicate upload finds the
existing documents row and writes nothing new — same blob key, zero new
rows, zero drift.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import psycopg

from docpipe.storage import DocumentStorage, LocalFSStorage


@dataclass(frozen=True)
class IngestResult:
    document_id: UUID
    sha256: str
    created: bool  # False = duplicate upload, existing row returned


def ingest(
    conn: psycopg.Connection,
    client_id: UUID,
    file_path: Path,
    source_channel: str,
    *,
    storage: DocumentStorage | None = None,
) -> IngestResult:
    """Store the file content-addressed and create/find its documents row."""
    storage = storage or LocalFSStorage()
    data = file_path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()

    existing = conn.execute(
        "SELECT id FROM documents WHERE client_id = %s AND sha256 = %s",
        (client_id, sha256),
    ).fetchone()
    if existing is not None:
        return IngestResult(document_id=existing[0], sha256=sha256, created=False)

    if not storage.exists(sha256):
        storage.put(sha256, data)

    content_type, _ = mimetypes.guess_type(file_path.name)
    row = conn.execute(
        """
        INSERT INTO documents (client_id, storage_ref, sha256, status,
                               filename, content_type, source_channel)
        VALUES (%s, %s, %s, 'received', %s, %s, %s)
        RETURNING id
        """,
        (client_id, sha256, sha256, file_path.name, content_type,
         source_channel),
    ).fetchone()
    assert row is not None
    conn.commit()
    return IngestResult(document_id=row[0], sha256=sha256, created=True)
