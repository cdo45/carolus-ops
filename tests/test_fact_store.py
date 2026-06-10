"""Fact store invariants, supersede chains, and trigger-backed immutability."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.errors import RaiseException

from knowledge.store import (
    InvalidFact,
    SupersedeError,
    add_fact,
    get_active_facts,
    get_fact_history,
    supersede_fact,
)
from tests.conftest import make_client


def crew_fact(conn: psycopg.Connection, client_id: UUID, **overrides: object):
    keyword_args: dict = dict(
        category="operations",
        statement="Runs two crews; second crew is subbed labor",
        source_type="email",
        source_ref="msg-1001",
    )
    keyword_args.update(overrides)
    return add_fact(conn, client_id, **keyword_args)


def test_add_fact_round_trip(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    fact = crew_fact(
        conn, client_id,
        confidence=Decimal("1.00"), effective_date=date(2026, 3, 1),
    )
    conn.commit()

    (loaded,) = get_active_facts(conn, client_id)
    assert loaded == fact
    assert loaded.status == "active"
    assert loaded.effective_date == date(2026, 3, 1)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"category": "financial"}, "category"),  # old vocabulary must fail
        ({"statement": ""}, "empty"),
        ({"statement": "x" * 201}, "201 chars"),
        ({"source_type": "slack"}, "source_type"),
        ({"source_ref": "   "}, "provenance"),
        ({"confidence": 1.5}, "confidence"),
    ],
)
def test_add_fact_rejects_invariant_violations(
    conn: psycopg.Connection, override: dict, match: str
) -> None:
    client_id = make_client(conn)
    with pytest.raises(InvalidFact, match=match):
        crew_fact(conn, client_id, **override)


def test_statement_is_trimmed_not_silently_truncated(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    fact = crew_fact(conn, client_id, statement="  padded statement  ")
    assert fact.statement == "padded statement"


def test_supersede_chain(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    original = crew_fact(conn, client_id)
    successor = supersede_fact(
        conn, original.id,
        statement="Runs three crews as of March; two subbed",
        source_type="email", source_ref="msg-2002",
        effective_date=date(2026, 3, 15),
    )
    conn.commit()

    assert successor.category == "operations", "category inherited from target"
    actives = get_active_facts(conn, client_id)
    assert [fact.id for fact in actives] == [successor.id]

    history = get_fact_history(conn, original.id)
    assert [fact.id for fact in history] == [original.id, successor.id]
    assert history[0].status == "superseded"
    assert history[0].superseded_by == successor.id
    assert get_fact_history(conn, successor.id) == history, (
        "chain identical from either end"
    )


def test_supersede_rejects_missing_and_inactive_targets(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    with pytest.raises(SupersedeError, match="does not exist"):
        supersede_fact(conn, uuid4(), statement="x",
                       source_type="carlos", source_ref="carlos:n1")

    original = crew_fact(conn, client_id)
    supersede_fact(conn, original.id, statement="replacement statement",
                   source_type="carlos", source_ref="carlos:n2")
    with pytest.raises(SupersedeError, match="superseded.*not active"):
        supersede_fact(conn, original.id, statement="second replacement",
                       source_type="carlos", source_ref="carlos:n3")


def test_superseded_rows_are_immutable_via_trigger(
    conn: psycopg.Connection,
) -> None:
    """The DB trigger — not politeness — guards retired facts."""
    client_id = make_client(conn)
    original = crew_fact(conn, client_id)
    supersede_fact(conn, original.id, statement="the new truth",
                   source_type="carlos", source_ref="carlos:n4")
    conn.commit()

    with pytest.raises(RaiseException, match="append-only"):
        conn.execute(
            "UPDATE facts SET statement = 'rewritten history' WHERE id = %s",
            (original.id,),
        )
    conn.rollback()
    with pytest.raises(RaiseException, match="append-only"):
        conn.execute("DELETE FROM facts WHERE id = %s", (original.id,))
    conn.rollback()


def test_active_ordering_is_total_and_category_first(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    crew_fact(conn, client_id, category="preferences",
              statement="Prefers Friday afternoon summaries")
    crew_fact(conn, client_id, category="entity_profile",
              statement="S-corp electrical contractor, 12 employees",
              effective_date=date(2026, 1, 1))
    crew_fact(conn, client_id, category="entity_profile",
              statement="Founded 2011, family-owned")  # NULL effective_date
    conn.commit()

    facts = get_active_facts(conn, client_id)
    assert [fact.category for fact in facts] == [
        "entity_profile", "entity_profile", "preferences",
    ], "taxonomy order wins over insertion order"
    assert facts[0].effective_date == date(2026, 1, 1), "dated before undated"

    with pytest.raises(InvalidFact):
        get_active_facts(conn, client_id, category="bogus")
