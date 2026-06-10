"""Validation gate for fact operations. NOTHING writes to the fact store
except operations that passed every check here (principle 2: validators
enforce, not prompts).

Checks run IN ORDER per operation; the first failure rejects the op with
a machine-readable reason code:

  1. schema       — pydantic: op add|supersede, category in taxonomy,
                    statement <= 200 chars, confidence stated|inferred,
                    effective_date None or ISO date, provenance present,
                    supersedes present iff op == supersede
  2. referential  — source_ref resolves to a real row for its source_type
  3. supersede    — target exists, is active, belongs to this client
  4. near-dup     — trigram similarity >= 0.6 (pg_trgm) against active
                    facts (and against ops already accepted in this batch)
                    rejects as duplicate, UNLESS the op supersedes that
                    very fact

The ops vocabulary's categorical confidence maps to the numeric facts
column as stated -> 1.00, inferred -> 0.50 (CONFIDENCE_NUMERIC).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, get_args
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from knowledge.store import CATEGORIES, MAX_STATEMENT_CHARS

DUPLICATE_SIMILARITY: float = 0.6
_CARLOS_REF = re.compile(r"^carlos:\S+$")
_QBO_COMPOSITE = re.compile(r"^qbo:(?P<txn_type>[A-Za-z]+):(?P<qbo_id>\S+)$")

# reason codes — machine-readable, stable
SCHEMA_INVALID = "schema_invalid"
SOURCE_NOT_FOUND = "source_not_found"
SUPERSEDE_TARGET_MISSING = "supersede_target_missing"
SUPERSEDE_TARGET_INACTIVE = "supersede_target_inactive"
SUPERSEDE_WRONG_CLIENT = "supersede_wrong_client"
DUPLICATE_FACT = "duplicate_fact"

CONFIDENCE_NUMERIC: dict[str, Decimal] = {
    "stated": Decimal("1.00"),
    "inferred": Decimal("0.50"),
}


class FactOperation(BaseModel):
    """The exact op shape the extraction prompt must emit."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "supersede"]
    category: Literal[  # mirrors knowledge.store.CATEGORIES
        "entity_profile",
        "operations",
        "accounting_policy",
        "relationships",
        "preferences",
        "watch_items",
        "resolved_history",
    ]
    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_CHARS)
    source_type: Literal["email", "qbo", "document", "carlos"]
    source_ref: str = Field(min_length=1)
    confidence: Literal["stated", "inferred"]
    effective_date: date | None = None
    supersedes: UUID | None = None

    @model_validator(mode="after")
    def _supersedes_iff_supersede(self) -> FactOperation:
        if self.op == "supersede" and self.supersedes is None:
            raise ValueError("op=supersede requires 'supersedes' fact id")
        if self.op == "add" and self.supersedes is not None:
            raise ValueError("op=add must not carry 'supersedes'")
        return self


# the Literal above must never drift from the store's taxonomy
assert get_args(FactOperation.model_fields["category"].annotation) == CATEGORIES


@dataclass(frozen=True)
class ValidOp:
    index: int
    operation: FactOperation


@dataclass(frozen=True)
class RejectedOp:
    index: int
    reason_code: str
    message: str
    raw: dict[str, Any]


@dataclass
class ValidationResult:
    valid: list[ValidOp] = field(default_factory=list)
    rejected: list[RejectedOp] = field(default_factory=list)

    @property
    def rejection_codes(self) -> list[str]:
        return [rejected.reason_code for rejected in self.rejected]


def _source_exists(
    conn: psycopg.Connection, client_id: UUID, source_type: str, source_ref: str
) -> bool:
    if source_type == "carlos":
        return _CARLOS_REF.match(source_ref) is not None
    if source_type == "email":
        return (
            conn.execute(
                "SELECT 1 FROM emails WHERE client_id = %s AND message_id = %s",
                (client_id, source_ref),
            ).fetchone()
            is not None
        )
    if source_type == "document":
        try:
            document_id = UUID(source_ref)
        except ValueError:
            return False
        return (
            conn.execute(
                "SELECT 1 FROM documents WHERE client_id = %s AND id = %s",
                (client_id, document_id),
            ).fetchone()
            is not None
        )
    if source_type == "qbo":
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM transactions WHERE client_id = %s AND id = %s",
                    (client_id, UUID(source_ref)),
                ).fetchone()
                is not None
            )
        except ValueError:
            pass
        composite = _QBO_COMPOSITE.match(source_ref)
        if composite is None:
            return False
        return (
            conn.execute(
                "SELECT 1 FROM transactions WHERE client_id = %s"
                " AND qbo_id = %s AND txn_type = %s",
                (client_id, composite["qbo_id"], composite["txn_type"]),
            ).fetchone()
            is not None
        )
    return False


def _nearest_active_duplicate(
    conn: psycopg.Connection, client_id: UUID, statement: str
) -> tuple[UUID, str, float] | None:
    row = conn.execute(
        """
        SELECT id, statement, similarity(statement, %(s)s) AS sim
        FROM facts
        WHERE client_id = %(client_id)s AND status = 'active'
          AND similarity(statement, %(s)s) >= %(threshold)s
        ORDER BY sim DESC, id
        LIMIT 1
        """,
        {"s": statement, "client_id": client_id,
         "threshold": DUPLICATE_SIMILARITY},
    ).fetchone()
    return None if row is None else (row[0], row[1], float(row[2]))


def _batch_duplicate(
    conn: psycopg.Connection, statement: str, accepted: list[FactOperation]
) -> str | None:
    for earlier in accepted:
        row = conn.execute(
            "SELECT similarity(%s, %s)", (statement, earlier.statement)
        ).fetchone()
        assert row is not None
        if float(row[0]) >= DUPLICATE_SIMILARITY:
            return earlier.statement
    return None


def validate_operations(
    conn: psycopg.Connection, client_id: UUID, ops_json: list[dict[str, Any]]
) -> ValidationResult:
    """Run the four-stage gate over a batch; never writes anything."""
    result = ValidationResult()
    for index, raw in enumerate(ops_json):
        # 1. schema
        try:
            operation = FactOperation.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first["loc"]) or "op"
            result.rejected.append(RejectedOp(
                index=index,
                reason_code=SCHEMA_INVALID,
                message=f"{location}: {first['msg']}",
                raw=raw if isinstance(raw, dict) else {"value": raw},
            ))
            continue

        # 2. referential — provenance must point at a real row
        if not _source_exists(
            conn, client_id, operation.source_type, operation.source_ref
        ):
            result.rejected.append(RejectedOp(
                index=index,
                reason_code=SOURCE_NOT_FOUND,
                message=(
                    f"{operation.source_type} ref {operation.source_ref!r}"
                    " does not resolve to a known row"
                ),
                raw=raw,
            ))
            continue

        # 3. supersede target
        if operation.op == "supersede":
            target = conn.execute(
                "SELECT client_id, status FROM facts WHERE id = %s",
                (operation.supersedes,),
            ).fetchone()
            if target is None:
                result.rejected.append(RejectedOp(
                    index=index, reason_code=SUPERSEDE_TARGET_MISSING,
                    message=f"fact {operation.supersedes} does not exist",
                    raw=raw,
                ))
                continue
            target_client, target_status = target
            if target_client != client_id:
                result.rejected.append(RejectedOp(
                    index=index, reason_code=SUPERSEDE_WRONG_CLIENT,
                    message=f"fact {operation.supersedes} belongs to another"
                            " client",
                    raw=raw,
                ))
                continue
            if target_status != "active":
                result.rejected.append(RejectedOp(
                    index=index, reason_code=SUPERSEDE_TARGET_INACTIVE,
                    message=f"fact {operation.supersedes} is {target_status},"
                            " not active",
                    raw=raw,
                ))
                continue

        # 4. near-duplicate (exempt: superseding that very fact)
        duplicate = _nearest_active_duplicate(conn, client_id, operation.statement)
        if duplicate is not None:
            duplicate_id, duplicate_statement, similarity_score = duplicate
            if not (operation.op == "supersede"
                    and operation.supersedes == duplicate_id):
                result.rejected.append(RejectedOp(
                    index=index, reason_code=DUPLICATE_FACT,
                    message=(
                        f"{similarity_score:.2f} similar to active fact"
                        f" {duplicate_id}: {duplicate_statement!r}"
                    ),
                    raw=raw,
                ))
                continue
        batch_twin = _batch_duplicate(
            conn, operation.statement,
            [valid.operation for valid in result.valid],
        )
        if batch_twin is not None:
            result.rejected.append(RejectedOp(
                index=index, reason_code=DUPLICATE_FACT,
                message=f"duplicates op in the same batch: {batch_twin!r}",
                raw=raw,
            ))
            continue

        result.valid.append(ValidOp(index=index, operation=operation))
    return result
