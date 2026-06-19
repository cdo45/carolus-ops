"""Database-enforced tenant isolation (RLS, migration 0013).

Proves carolus_app can never cross a client boundary: a scoped read sees
only its own client, a cross-tenant write is rejected by WITH CHECK, and an
unscoped read fails closed (zero rows). The pipeline's owner connection
(the `conn` fixture) bypasses RLS, so this is the only suite that exercises
the carolus_app path.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

from db.tenant import tenant_tx


def _client(conn: psycopg.Connection, name: str) -> UUID:
    row = conn.execute(
        "INSERT INTO clients (name) VALUES (%s) RETURNING id", (name,)
    ).fetchone()
    assert row is not None
    return row[0]


def _flag(conn: psycopg.Connection, client_id: UUID) -> None:
    conn.execute(
        "INSERT INTO flags (client_id, rule_code, severity, source_type,"
        " source_ref) VALUES (%s, 'R000', 'warn', 'transaction', 'x')",
        (client_id,),
    )


@pytest.fixture
def two_clients(conn: psycopg.Connection) -> tuple[UUID, UUID]:
    """Clients A and B, each with one flag (seeded as the owner)."""
    client_a, client_b = _client(conn, "Client A"), _client(conn, "Client B")
    _flag(conn, client_a)
    _flag(conn, client_b)
    conn.commit()
    return client_a, client_b


def test_scoped_query_sees_only_its_client(
    conn: psycopg.Connection, two_clients: tuple[UUID, UUID]
) -> None:
    client_a, _client_b = two_clients
    with tenant_tx(conn, client_a):
        rows = conn.execute("SELECT client_id FROM flags").fetchall()
    assert [row[0] for row in rows] == [client_a], "sees A's row, zero of B's"


def test_cross_tenant_write_blocked(
    conn: psycopg.Connection, two_clients: tuple[UUID, UUID]
) -> None:
    client_a, client_b = two_clients
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        # write B's client_id while scoped to A → WITH CHECK rejects it
        with tenant_tx(conn, client_a):
            conn.execute(
                "INSERT INTO flags (client_id, rule_code, severity,"
                " source_type, source_ref)"
                " VALUES (%s, 'R000', 'warn', 'transaction', 'y')",
                (client_b,),
            )


def test_unscoped_fails_closed(
    conn: psycopg.Connection, two_clients: tuple[UUID, UUID]
) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL ROLE carolus_app")  # carolus_app, no GUC set
        rows = conn.execute("SELECT 1 FROM flags").fetchall()
    assert rows == [], "unset GUC → policy predicate NULL → zero rows"
