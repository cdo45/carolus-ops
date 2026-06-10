"""Every validator reason code proven by a rejecting fixture, plus a clean
batch passing end-to-end. DB-backed (pg_trgm similarity is SQL)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from knowledge.store import add_fact
from knowledge.validator import (
    DUPLICATE_FACT,
    SCHEMA_INVALID,
    SOURCE_NOT_FOUND,
    SUPERSEDE_TARGET_INACTIVE,
    SUPERSEDE_TARGET_MISSING,
    SUPERSEDE_WRONG_CLIENT,
    validate_operations,
)
from tests.conftest import make_client
from tests.factories import make_account, make_txn


def op(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "op": "add",
        "category": "operations",
        "statement": "Crews start at 6am during summer schedule",
        "source_type": "carlos",
        "source_ref": "carlos:note-1",
        "confidence": "stated",
    }
    base.update(overrides)
    return base


@pytest.fixture
def client_id(conn: psycopg.Connection) -> UUID:
    return make_client(conn)


def seed_email(conn: psycopg.Connection, client_id: UUID, message_id: str) -> None:
    conn.execute(
        """
        INSERT INTO emails (client_id, direction, message_id, subject)
        VALUES (%s, 'inbound', %s, 'about the kitchen job')
        """,
        (client_id, message_id),
    )
    conn.commit()


# ------------------------------------------------------------ schema stage


@pytest.mark.parametrize(
    "bad",
    [
        op(op="delete"),
        op(category="financial"),  # pre-taxonomy vocabulary
        op(statement="x" * 201),
        op(statement=""),
        op(confidence="certain"),
        op(effective_date="not-a-date"),
        op(source_ref=""),
        {"op": "add"},  # missing nearly everything
        op(op="supersede"),  # supersede without target
        op(supersedes=str(uuid4())),  # add carrying supersedes
        op(extra_field="nope"),  # extra="forbid"
    ],
)
def test_schema_violations_rejected(
    conn: psycopg.Connection, client_id: UUID, bad: dict[str, Any]
) -> None:
    result = validate_operations(conn, client_id, [bad])
    assert result.valid == []
    assert result.rejection_codes == [SCHEMA_INVALID]
    assert result.rejected[0].message, "rejection carries a human message"


# ------------------------------------------------------------ referential


def test_unresolvable_refs_rejected(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    cases = [
        op(source_type="email", source_ref="msg-does-not-exist"),
        op(source_type="document", source_ref=str(uuid4())),
        op(source_type="document", source_ref="not-even-a-uuid"),
        op(source_type="qbo", source_ref=str(uuid4())),
        op(source_type="qbo", source_ref="qbo:Invoice:99999"),
        op(source_type="qbo", source_ref="garbled-ref"),
        op(source_type="carlos", source_ref="note-7"),  # missing carlos: prefix
    ]
    result = validate_operations(conn, client_id, cases)
    assert result.valid == []
    assert result.rejection_codes == [SOURCE_NOT_FOUND] * len(cases)


def test_refs_resolve_per_source_type(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    seed_email(conn, client_id, "msg-100")
    expense = make_account(conn, client_id)
    txn_id = make_txn(conn, client_id, qbo_id="555", txn_type="Bill",
                      amount="10.00",
                      lines=[{"account": expense, "amount": "10.00"}])
    document = conn.execute(
        "INSERT INTO documents (client_id, storage_ref, sha256)"
        " VALUES (%s, 'r2://x', 'abc') RETURNING id",
        (client_id,),
    ).fetchone()
    assert document is not None
    conn.commit()

    statements = iter([
        "Email-sourced fact about crew scheduling",
        "Transaction-sourced fact about equipment financing",
        "Composite-ref fact about the lumber bill terms",
        "Document-sourced fact regarding insurance coverage",
        "Carlos-sourced fact on client communication cadence",
    ])
    cases = [
        op(source_type="email", source_ref="msg-100", statement=next(statements)),
        op(source_type="qbo", source_ref=str(txn_id), statement=next(statements)),
        op(source_type="qbo", source_ref="qbo:Bill:555", statement=next(statements)),
        op(source_type="document", source_ref=str(document[0]),
           statement=next(statements)),
        op(source_type="carlos", source_ref="carlos:meeting-2026-06-01",
           statement=next(statements)),
    ]
    result = validate_operations(conn, client_id, cases)
    assert result.rejected == []
    assert len(result.valid) == 5


# ------------------------------------------------------------ supersede


def test_supersede_target_checks(conn: psycopg.Connection, client_id: UUID) -> None:
    mine = add_fact(conn, client_id, category="operations",
                    statement="Uses three subcontracted framing crews",
                    source_type="carlos", source_ref="carlos:n1")
    other_client = make_client(conn, realm="other-realm")
    theirs = add_fact(conn, other_client, category="operations",
                      statement="Completely unrelated other-client fact",
                      source_type="carlos", source_ref="carlos:n2")
    retired = add_fact(conn, client_id, category="preferences",
                       statement="Wants paper copies of every invoice",
                       source_type="carlos", source_ref="carlos:n3")
    conn.execute(
        "UPDATE facts SET status='superseded', superseded_by=%s WHERE id=%s",
        (mine.id, retired.id),
    )
    conn.commit()

    def supersede_of(target: UUID, statement: str) -> dict[str, Any]:
        return op(op="supersede", supersedes=str(target), statement=statement)

    result = validate_operations(conn, client_id, [
        supersede_of(uuid4(), "Replacement for a fact that is not there"),
        supersede_of(theirs.id, "Replacement reaching across clients"),
        supersede_of(retired.id, "Replacement for an already retired fact"),
        supersede_of(mine.id, "Uses four subcontracted framing crews now"),
    ])
    assert result.rejection_codes == [
        SUPERSEDE_TARGET_MISSING,
        SUPERSEDE_WRONG_CLIENT,
        SUPERSEDE_TARGET_INACTIVE,
    ]
    assert [valid.index for valid in result.valid] == [3]


# ------------------------------------------------------------ near-duplicate


def test_duplicate_against_active_facts(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    existing = add_fact(
        conn, client_id, category="operations",
        statement="Foreman sends receipt photos every Friday afternoon",
        source_type="carlos", source_ref="carlos:n1",
    )
    conn.commit()

    near_copy = op(statement="Foreman sends receipt photos every Friday")
    unrelated = op(statement="Retainage on county jobs is held at ten percent")
    update_same = op(op="supersede", supersedes=str(existing.id),
                     statement="Foreman sends receipt photos every Monday"
                               " afternoon")

    result = validate_operations(conn, client_id,
                                 [near_copy, unrelated, update_same])
    assert result.rejection_codes == [DUPLICATE_FACT]
    assert result.rejected[0].index == 0
    assert str(existing.id) in result.rejected[0].message
    assert [valid.index for valid in result.valid] == [1, 2], (
        "superseding the matched fact itself is NOT a duplicate"
    )


def test_duplicate_within_same_batch(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    first = op(statement="Equipment loans run through Farm Credit leasing")
    twin = op(statement="Equipment loans run through Farm Credit leasing arm")
    result = validate_operations(conn, client_id, [first, twin])
    assert [valid.index for valid in result.valid] == [0]
    assert result.rejection_codes == [DUPLICATE_FACT]
    assert "same batch" in result.rejected[0].message


# ------------------------------------------------------------ ordering


def test_checks_run_in_order_first_failure_wins(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    add_fact(conn, client_id, category="operations",
             statement="Crews start at 6am during summer schedule",
             source_type="carlos", source_ref="carlos:n1")
    conn.commit()
    # would be a duplicate AND has a bad ref AND a bad category:
    # schema must win
    bad = op(category="bogus", source_ref="carlos-bad-format")
    result = validate_operations(conn, client_id, [bad])
    assert result.rejection_codes == [SCHEMA_INVALID]

    # schema fine; ref broken AND duplicate: referential wins
    bad_ref = op(source_type="email", source_ref="msg-none")
    result = validate_operations(conn, client_id, [bad_ref])
    assert result.rejection_codes == [SOURCE_NOT_FOUND]
