"""Shared fixtures for DB-backed tests.

`conn` provides a connection to a scratch Postgres database whose schema is
DROPPED and rebuilt (all migrations) per test. Skipped unless
CAROLUS_TEST_DB is set — plain CI runs only the pure unit tests.

Run locally:
    CAROLUS_TEST_DB=postgresql://carolus:...@localhost:5432/carolus_test \
        uv run pytest
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest

from db.migrate import migrate


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    url = os.environ.get("CAROLUS_TEST_DB")
    if not url:
        pytest.skip("CAROLUS_TEST_DB not set (scratch database required)")
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    migrate(url)
    with psycopg.connect(url) as connection:
        yield connection


def make_client(conn: psycopg.Connection, realm: str = "test-realm-1") -> UUID:
    row = conn.execute(
        "INSERT INTO clients (name, qbo_realm_id) VALUES (%s, %s) RETURNING id",
        ("Fixture Co", realm),
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]
