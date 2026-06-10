"""Fact store: the ONLY write path into the facts table.

Facts are append-only with provenance (principle 2): every fact carries a
source_type + source_ref, statements are atomic (<= 200 chars), and
updates happen by SUPERSEDING — the old row keeps its history, flips to
status='superseded', and points at its replacement. A DB trigger
(facts_append_only) makes superseded content immutable at the engine
level; this module enforces the same invariants at the API level by
RAISING — it never silently fixes input.

Categories are the agreed knowledge taxonomy (migration 0007). The
numeric confidence column stores the ops vocabulary mapped as
stated -> 1.00, inferred -> 0.50 (see knowledge.validator).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import psycopg

CATEGORIES: tuple[str, ...] = (
    "entity_profile",
    "operations",
    "accounting_policy",
    "relationships",
    "preferences",
    "watch_items",
    "resolved_history",
)
SOURCE_TYPES: tuple[str, ...] = ("email", "qbo", "document", "carlos")
MAX_STATEMENT_CHARS: int = 200

_FACT_COLUMNS = """
    id, client_id, category, statement, source_type, source_ref,
    confidence, status, superseded_by, effective_date, created_at
"""


class FactError(Exception):
    """Base for fact-store violations."""


class InvalidFact(FactError):
    """Input violates a fact invariant (category, length, provenance…)."""


class SupersedeError(FactError):
    """Supersede target missing, inactive, or otherwise unusable."""


@dataclass(frozen=True)
class Fact:
    id: UUID
    client_id: UUID
    category: str
    statement: str
    source_type: str
    source_ref: str
    confidence: Decimal | None
    status: str
    superseded_by: UUID | None
    effective_date: date | None
    created_at: datetime


def _fact_from_row(row: tuple) -> Fact:  # type: ignore[type-arg]
    return Fact(*row)


def _check_invariants(
    category: str,
    statement: str,
    source_type: str,
    source_ref: str,
    confidence: Decimal | float | None,
) -> str:
    """Validate shared fact invariants; returns the normalized statement."""
    if category not in CATEGORIES:
        raise InvalidFact(f"category {category!r} not in {CATEGORIES}")
    normalized = statement.strip()
    if not normalized:
        raise InvalidFact("statement is empty")
    if len(normalized) > MAX_STATEMENT_CHARS:
        raise InvalidFact(
            f"statement is {len(normalized)} chars"
            f" (max {MAX_STATEMENT_CHARS}): {normalized[:60]!r}…"
        )
    if source_type not in SOURCE_TYPES:
        raise InvalidFact(f"source_type {source_type!r} not in {SOURCE_TYPES}")
    if not str(source_ref).strip():
        raise InvalidFact("source_ref is empty — no fact without provenance")
    if confidence is not None and not (0 <= Decimal(str(confidence)) <= 1):
        raise InvalidFact(f"confidence {confidence} outside [0, 1]")
    return normalized


def add_fact(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    category: str,
    statement: str,
    source_type: str,
    source_ref: str,
    confidence: Decimal | float | None = None,
    effective_date: date | None = None,
) -> Fact:
    """Insert a new active fact; raises InvalidFact on any violation."""
    normalized = _check_invariants(
        category, statement, source_type, source_ref, confidence
    )
    row = conn.execute(
        f"""
        INSERT INTO facts (client_id, category, statement, source_type,
                           source_ref, confidence, effective_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING {_FACT_COLUMNS}
        """,
        (client_id, category, normalized, source_type, str(source_ref).strip(),
         confidence, effective_date),
    ).fetchone()
    assert row is not None
    return _fact_from_row(row)


def supersede_fact(
    conn: psycopg.Connection,
    target_fact_id: UUID,
    *,
    statement: str,
    source_type: str,
    source_ref: str,
    category: str | None = None,
    confidence: Decimal | float | None = None,
    effective_date: date | None = None,
) -> Fact:
    """Replace an active fact: insert the successor, retire the target.

    The target must exist and be active. category defaults to the
    target's. Returns the new active fact.
    """
    target_row = conn.execute(
        f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = %s FOR UPDATE",
        (target_fact_id,),
    ).fetchone()
    if target_row is None:
        raise SupersedeError(f"supersede target {target_fact_id} does not exist")
    target = _fact_from_row(target_row)
    if target.status != "active":
        raise SupersedeError(
            f"supersede target {target_fact_id} is {target.status!r}, not active"
        )

    successor = add_fact(
        conn,
        target.client_id,
        category=category or target.category,
        statement=statement,
        source_type=source_type,
        source_ref=source_ref,
        confidence=confidence,
        effective_date=effective_date,
    )
    conn.execute(
        """
        UPDATE facts SET status = 'superseded', superseded_by = %s
        WHERE id = %s AND status = 'active'
        """,
        (successor.id, target_fact_id),
    )
    return successor


def get_active_facts(
    conn: psycopg.Connection, client_id: UUID, category: str | None = None
) -> list[Fact]:
    """Active facts in a total, deterministic order (render relies on it)."""
    if category is not None and category not in CATEGORIES:
        raise InvalidFact(f"category {category!r} not in {CATEGORIES}")
    rows = conn.execute(
        f"""
        SELECT {_FACT_COLUMNS} FROM facts
        WHERE client_id = %s AND status = 'active'
          AND (%s::text IS NULL OR category = %s)
        ORDER BY array_position(%s::text[], category),
                 effective_date NULLS LAST, created_at, id
        """,
        (client_id, category, category, list(CATEGORIES)),
    ).fetchall()
    return [_fact_from_row(row) for row in rows]


def get_fact_history(conn: psycopg.Connection, fact_id: UUID) -> list[Fact]:
    """The full supersede chain containing fact_id, oldest first."""
    row = conn.execute(
        f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = %s", (fact_id,)
    ).fetchone()
    if row is None:
        raise FactError(f"fact {fact_id} does not exist")
    current = _fact_from_row(row)

    chain = [current]
    # walk backward: predecessors point AT the head via superseded_by
    while True:
        prev = conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE superseded_by = %s"
            " ORDER BY created_at LIMIT 1",
            (chain[0].id,),
        ).fetchone()
        if prev is None:
            break
        chain.insert(0, _fact_from_row(prev))
    # walk forward: follow superseded_by links
    while chain[-1].superseded_by is not None:
        nxt = conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = %s",
            (chain[-1].superseded_by,),
        ).fetchone()
        if nxt is None:  # dangling pointer would be a bug, surface loudly
            raise FactError(
                f"fact {chain[-1].id} points at missing successor"
                f" {chain[-1].superseded_by}"
            )
        chain.append(_fact_from_row(nxt))
    return chain
