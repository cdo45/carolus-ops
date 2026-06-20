"""The Phase 5 hard wall: carolus_agent is structurally confined to one client
by Postgres RLS (migration 0015), including journal_lines — the only tenant
table without a client_id, scoped through transaction_id -> transactions.

Aggressive about breaking through: two clients with full chains, then as
carolus_agent scoped to A we prove reads see only A (journal_lines via the
parent join), cross-tenant writes are rejected by WITH CHECK, an unscoped
agent fails closed, and no tenant table is reachable without a policy. The
owner connection (the conn fixture) bypasses RLS, so this is the only suite
exercising the carolus_agent path.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from tests.factories import make_account, make_txn


@dataclass(frozen=True)
class Chain:
    client: UUID
    account: UUID
    txn: UUID
    line_ids: frozenset[UUID]
    flag: UUID


def _seed_chain(conn: psycopg.Connection, *, name: str) -> Chain:
    """A full client chain seeded as the owner: a transaction with journal
    lines, a flag, and a review_queue item."""
    row = conn.execute(
        "INSERT INTO clients (name) VALUES (%s) RETURNING id", (name,)
    ).fetchone()
    assert row is not None
    client = row[0]
    account = make_account(conn, client, name="Checking", acct_type="Bank")
    txn = make_txn(
        conn, client, txn_type="Deposit", amount="100.00",
        lines=[{"account": account, "amount": "100.00", "posting": "debit"},
               {"account": account, "amount": "100.00", "posting": "credit"}],
    )
    line_ids = frozenset(
        r[0] for r in conn.execute(
            "SELECT id FROM journal_lines WHERE transaction_id = %s", (txn,)
        ).fetchall()
    )
    flag_row = conn.execute(
        "INSERT INTO flags (client_id, rule_code, severity, source_type,"
        " source_ref) VALUES (%s, 'R000', 'warn', 'transaction', 'x') RETURNING id",
        (client,),
    ).fetchone()
    assert flag_row is not None
    conn.execute(
        "INSERT INTO review_queue (client_id, kind, source_type, source_ref, title)"
        " VALUES (%s, 'flag', 'flag', %s, 'needs review')",
        (client, str(flag_row[0])),
    )
    return Chain(client=client, account=account, txn=txn, line_ids=line_ids,
                 flag=flag_row[0])


@pytest.fixture
def ab(conn: psycopg.Connection) -> tuple[Chain, Chain]:
    """Clients A and B, each a full chain (seeded as the owner)."""
    a, b = _seed_chain(conn, name="Client A"), _seed_chain(conn, name="Client B")
    conn.commit()
    return a, b


@contextmanager
def _as_agent(
    conn: psycopg.Connection, client_id: UUID | None = None
) -> Iterator[psycopg.Connection]:
    """One transaction as carolus_agent, optionally scoped to a client."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE carolus_agent")
        if client_id is not None:
            conn.execute(
                "SELECT set_config('app.current_client', %s, true)", (str(client_id),)
            )
        yield conn


def _client_id_tables(conn: psycopg.Connection) -> list[str]:
    """Every base table in public with a non-dropped client_id column."""
    return [
        r[0] for r in conn.execute(
            """
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                          AND a.attname = 'client_id' AND a.attnum > 0
                          AND NOT a.attisdropped)
            ORDER BY c.relname
            """
        ).fetchall()
    ]


def test_agent_reads_only_its_client(
    conn: psycopg.Connection, ab: tuple[Chain, Chain]
) -> None:
    a, b = ab
    tables = _client_id_tables(conn)
    with _as_agent(conn, a.client):
        assert {r[0] for r in conn.execute("SELECT id FROM clients")} == {a.client}, (
            "the root: A sees only its own client row"
        )
        for t in tables:
            seen = {
                r[0] for r in conn.execute(
                    sql.SQL("SELECT DISTINCT client_id FROM {}").format(
                        sql.Identifier(t)
                    )
                )
            }
            assert b.client not in seen, f"{t} leaked B's rows to A"
            assert seen <= {a.client}, f"{t} exposed a foreign client to A"
        # journal_lines has no client_id: isolation is via the parent join.
        visible = {r[0] for r in conn.execute("SELECT id FROM journal_lines")}
        assert visible == set(a.line_ids), "journal_lines: exactly A's lines"
        assert not (visible & set(b.line_ids)), "journal_lines: none of B's lines"


def test_agent_cross_tenant_writes_rejected(
    conn: psycopg.Connection, ab: tuple[Chain, Chain]
) -> None:
    a, b = ab
    a_line = next(iter(a.line_ids))

    # journal_lines INSERT whose parent transaction belongs to B → WITH CHECK
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _as_agent(conn, a.client):
            conn.execute(
                "INSERT INTO journal_lines (transaction_id, line_no, account_id,"
                " amount, posting_type) VALUES (%s, 99, %s, '5.00', 'debit')",
                (b.txn, a.account),
            )
    # journal_lines UPDATE re-pointing one of A's lines into B's transaction
    # (line_no 99 is free in B's txn, so only the parent predicate can fail)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _as_agent(conn, a.client):
            conn.execute(
                "UPDATE journal_lines SET transaction_id = %s, line_no = 99"
                " WHERE id = %s",
                (b.txn, a_line),
            )
    # direct client_id table — INSERT B's row while scoped to A
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _as_agent(conn, a.client):
            conn.execute(
                "INSERT INTO flags (client_id, rule_code, severity, source_type,"
                " source_ref) VALUES (%s, 'R000', 'warn', 'transaction', 'y')",
                (b.client,),
            )
    # direct client_id table — UPDATE A's row to belong to B
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _as_agent(conn, a.client):
            conn.execute(
                "UPDATE flags SET client_id = %s WHERE id = %s", (b.client, a.flag)
            )


def test_agent_unscoped_fails_closed(
    conn: psycopg.Connection, ab: tuple[Chain, Chain]
) -> None:
    tables = _client_id_tables(conn)
    with _as_agent(conn):  # carolus_agent, no GUC
        assert conn.execute("SELECT count(*) FROM clients").fetchone() == (0,), (
            "the root fails closed without a GUC"
        )
        for t in tables:
            count = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
            ).fetchone()
            assert count == (0,), f"{t} is not fail-closed without a GUC"
        assert conn.execute("SELECT count(*) FROM journal_lines").fetchone() == (0,), (
            "journal_lines fails closed without a GUC"
        )


def test_every_tenant_table_walls_carolus_agent(conn: psycopg.Connection) -> None:
    """Standing gate: every base table holding client data (the client_id
    tables, plus journal_lines, plus the clients root) must have RLS enabled
    AND a tenant_isolation policy applying to carolus_agent. A future
    parent-scoped table added without a policy is a silent cross-tenant hole;
    this fails until the author wires it in on purpose."""
    rows = conn.execute(
        """
        SELECT c.relname, c.relrowsecurity,
               EXISTS (SELECT 1 FROM pg_policies pp
                       WHERE pp.schemaname = 'public' AND pp.tablename = c.relname
                         AND pp.policyname = 'tenant_isolation'
                         AND 'carolus_agent' = ANY(pp.roles))
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND (c.relname IN ('clients', 'journal_lines')
               OR EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                          AND a.attname = 'client_id' AND a.attnum > 0
                          AND NOT a.attisdropped))
        ORDER BY c.relname
        """
    ).fetchall()
    assert len(rows) >= 3, "introspection found too few tenant tables — query broke"
    offenders = [name for name, rls_on, agent_policy in rows
                 if not (rls_on and agent_policy)]
    assert not offenders, (
        f"tenant tables not walled for carolus_agent: {offenders} — every base"
        " table holding client data (client_id tables + journal_lines) needs RLS"
        " enabled AND a tenant_isolation policy applying to carolus_agent"
        " (see 0015_agent_role.sql); never weaken this guard"
    )
