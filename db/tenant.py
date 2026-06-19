"""Tenant-scoped database access through the least-privilege carolus_app role.

The portal/agent connect as carolus_app, whose RLS policies (migration 0013)
restrict every query to the client named by the app.current_client GUC.
tenant_tx scopes one transaction: it switches into carolus_app and binds the
GUC for the transaction's lifetime (both reset on commit/rollback via SET
LOCAL), so isolation is enforced by Postgres, not by application code.
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
