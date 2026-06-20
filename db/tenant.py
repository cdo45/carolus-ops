"""Tenant-scoped database access through the least-privilege RLS roles.

Two helpers, two scopings — isolation enforced by Postgres (migrations
0013/0015/0016), not by application code:

  - tenant_tx (carolus_app): scopes ONE transaction. SET LOCAL ROLE + a
    transaction-local GUC, both reset on commit/rollback. For the portal's
    request/response work, which does not commit mid-flight.

  - agent_connection (carolus_agent): scopes a whole CONNECTION at SESSION
    level (SET ROLE, set_config is_local=false), committed so it SURVIVES the
    internal commits the pipeline steps make (run_rules commits its own run;
    sync advances its cursor). A SET LOCAL scoping would drop back to the owner
    the moment a step commits, so the nightly routine's data steps use this.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg


@contextmanager
def tenant_tx(
    conn: psycopg.Connection, client_id: UUID
) -> Iterator[psycopg.Connection]:
    """Run a transaction as carolus_app scoped to one client (RLS-enforced)."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE carolus_app")
        conn.execute(
            "SELECT set_config('app.current_client', %s, true)", (str(client_id),)
        )
        yield conn


@contextmanager
def agent_connection(
    database_url: str, client_id: UUID
) -> Iterator[psycopg.Connection]:
    """A dedicated carolus_agent connection, SESSION-scoped to one client.

    The role and tenant GUC are set at SESSION level — SET ROLE (not SET LOCAL)
    and set_config(..., is_local=false) — and committed, so they survive the
    internal commits the data steps make; an internal commit would clear a
    transaction-scoped (tenant_tx) scoping and drop back to the owner mid-flow.
    Closing the connection resets both. Everything run on it is RLS-confined to
    client_id — including journal_lines, via its parent-scoped policy.
    """
    conn = psycopg.connect(database_url)
    try:
        conn.execute(
            "SELECT set_config('app.current_client', %s, false)", (str(client_id),)
        )
        conn.execute("SET ROLE carolus_agent")
        conn.commit()  # make the session role + GUC durable across step commits
        yield conn
    finally:
        conn.close()
