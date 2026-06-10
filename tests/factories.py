"""Canonical-row factories for rule tests.

Rules read canonical tables only, so tests seed those tables directly —
no QBO payloads or sync machinery needed. Callers commit.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

_seq = itertools.count(1)


def next_qbo_id(prefix: str = "F") -> str:
    return f"{prefix}{next(_seq):05d}"


def make_account(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    name: str = "Test Account",
    acct_type: str = "Expense",
    acct_subtype: str | None = None,
    active: bool = True,
    qbo_deleted_at: datetime | None = None,
) -> UUID:
    row = conn.execute(
        """
        INSERT INTO accounts (client_id, qbo_id, name, acct_type, acct_subtype,
                              active, qbo_deleted_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (client_id, next_qbo_id("A"), name, acct_type, acct_subtype, active,
         qbo_deleted_at),
    ).fetchone()
    assert row is not None
    return row[0]


def make_entity(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    kind: str = "vendor",
    name: str = "Test Vendor",
    active: bool = True,
) -> UUID:
    row = conn.execute(
        """
        INSERT INTO entities (client_id, qbo_id, kind, name, active)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (client_id, next_qbo_id("E"), kind, name, active),
    ).fetchone()
    assert row is not None
    return row[0]


def make_job(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    entity_id: UUID | None = None,
    name: str = "Test Job",
    status: str = "active",
) -> UUID:
    row = conn.execute(
        """
        INSERT INTO jobs (client_id, qbo_id, entity_id, name, status)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (client_id, next_qbo_id("J"), entity_id, name, status),
    ).fetchone()
    assert row is not None
    return row[0]


def make_txn(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    txn_type: str = "Purchase",
    txn_date: date | None = None,
    amount: Decimal | str | float | None = None,
    entity_id: UUID | None = None,
    qbo_id: str | None = None,
    doc_number: str | None = None,
    qbo_created_at: datetime | None = None,
    has_linked_txn: bool | None = None,
    lines: Sequence[dict[str, Any]] = (),
) -> UUID:
    """Insert a transaction plus journal lines.

    Each line dict: account (UUID, required), amount, posting ('debit'/
    'credit'), optional job and description. amount defaults to the sum
    of debit lines.
    """
    if amount is None:
        amount = sum(
            (Decimal(str(line["amount"])) for line in lines
             if line.get("posting", "debit") == "debit"),
            Decimal("0"),
        )
    row = conn.execute(
        """
        INSERT INTO transactions (client_id, qbo_id, txn_type, txn_date,
                                  amount, entity_id, doc_number,
                                  qbo_created_at, has_linked_txn)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (client_id, qbo_id or next_qbo_id("T"), txn_type,
         txn_date or date(2026, 5, 15), Decimal(str(amount)), entity_id,
         doc_number, qbo_created_at, has_linked_txn),
    ).fetchone()
    assert row is not None
    txn_id: UUID = row[0]
    for line_no, line in enumerate(lines):
        conn.execute(
            """
            INSERT INTO journal_lines (transaction_id, line_no, account_id,
                                       job_id, amount, posting_type, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (txn_id, line_no, line["account"], line.get("job"),
             Decimal(str(line["amount"])), line.get("posting", "debit"),
             line.get("description")),
        )
    return txn_id


def balanced_purchase(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    bank: UUID,
    expense: UUID,
    amount: str = "100.00",
    txn_date: date | None = None,
    entity_id: UUID | None = None,
    job: UUID | None = None,
    txn_type: str = "Purchase",
    has_linked_txn: bool | None = None,
) -> UUID:
    """The common case: one debit to expense, one credit from bank."""
    return make_txn(
        conn, client_id, txn_type=txn_type, txn_date=txn_date,
        entity_id=entity_id, amount=amount, has_linked_txn=has_linked_txn,
        lines=[
            {"account": expense, "amount": amount, "posting": "debit", "job": job},
            {"account": bank, "amount": amount, "posting": "credit"},
        ],
    )
