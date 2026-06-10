"""Extraction pipeline plumbing: prompt assembly in, validated ops out.

No live LLM call happens in this module (or this phase). The model is a
replaceable component behind two pure seams:

  assemble_extraction_input  — builds the exact payload the model gets:
      the versioned prompt (prompts/fact_extractor.md), the client's
      active facts (with ids, so the model can supersede), and ONE
      source item with the provenance ref it must echo.

  process_extraction_output  — takes the model's raw text, strips
      markdown fences, parses JSON, runs EVERY operation through
      knowledge.validator (nothing writes without passing), applies the
      passing ops via knowledge.store, logs a runs row, and returns
      (applied, rejected, uncertainties). Unparseable or contract-
      breaking output returns needs_retry=True and writes NOTHING —
      free-text model output is never parsed into the pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from knowledge import store, validator
from knowledge.validator import CONFIDENCE_NUMERIC, RejectedOp

PROMPT_PATH: Path = (
    Path(__file__).resolve().parent.parent / "prompts" / "fact_extractor.md"
)
PROMPT_VERSION: str = "fact_extractor v1.1"

_FENCE = re.compile(r"^```[a-zA-Z]*\n(?P<body>.*)\n```$", re.DOTALL)


@dataclass(frozen=True)
class SourceItem:
    """One item to extract from; source_ref is the provenance contract."""

    source_type: str  # email | qbo | document | carlos
    source_ref: str
    content: str


@dataclass
class ExtractionResult:
    applied: list[UUID] = field(default_factory=list)
    rejected: list[RejectedOp] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    needs_retry: bool = False
    error: str | None = None


def assemble_extraction_input(
    conn: psycopg.Connection, client_id: UUID, source: SourceItem
) -> dict[str, Any]:
    """The exact prompt payload for one extraction call."""
    active = store.get_active_facts(conn, client_id)
    return {
        "prompt_version": PROMPT_VERSION,
        "system": PROMPT_PATH.read_text(),
        "input": {
            "active_facts": [
                {"id": str(fact.id), "category": fact.category,
                 "statement": fact.statement}
                for fact in active
            ],
            "source": {
                "source_type": source.source_type,
                "source_ref": source.source_ref,
                "content": source.content,
            },
        },
    }


def strip_fences(raw: str) -> str:
    text = raw.strip()
    match = _FENCE.match(text)
    return match["body"].strip() if match else text


def _parse_model_output(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Returns (payload, error). Contract: a JSON object with an
    'operations' list (and optional 'uncertainties' list of strings)."""
    try:
        parsed = json.loads(strip_fences(raw))
    except json.JSONDecodeError as exc:
        return None, f"malformed_json: {exc.msg} at char {exc.pos}"
    if not isinstance(parsed, dict):
        return None, "contract_violation: top level is not a JSON object"
    operations = parsed.get("operations")
    if not isinstance(operations, list):
        return None, "contract_violation: missing 'operations' list"
    uncertainties = parsed.get("uncertainties", [])
    if not isinstance(uncertainties, list) or any(
        not isinstance(item, str) for item in uncertainties
    ):
        return None, "contract_violation: 'uncertainties' must be a string list"
    return parsed, None


def process_extraction_output(
    conn: psycopg.Connection, client_id: UUID, raw_json: str
) -> ExtractionResult:
    """Validate-then-apply one model output; logs a runs row either way."""
    run_row = conn.execute(
        "INSERT INTO runs (client_id, routine) VALUES (%s, 'fact_extraction')"
        " RETURNING id",
        (client_id,),
    ).fetchone()
    assert run_row is not None
    run_id: UUID = run_row[0]
    conn.commit()

    payload, parse_error = _parse_model_output(raw_json)
    if payload is None:
        conn.execute(
            "UPDATE runs SET finished_at = now(), status = 'failed',"
            " actions = %s WHERE id = %s",
            (Jsonb({"error": parse_error, "needs_retry": True}), run_id),
        )
        conn.commit()
        return ExtractionResult(needs_retry=True, error=parse_error)

    result = ExtractionResult(
        uncertainties=[str(item) for item in payload.get("uncertainties", [])]
    )
    validation = validator.validate_operations(
        conn, client_id, payload["operations"]
    )
    result.rejected = validation.rejected

    for valid in validation.valid:
        operation = valid.operation
        confidence = CONFIDENCE_NUMERIC[operation.confidence]
        if operation.op == "add":
            fact = store.add_fact(
                conn, client_id,
                category=operation.category,
                statement=operation.statement,
                source_type=operation.source_type,
                source_ref=operation.source_ref,
                confidence=confidence,
                effective_date=operation.effective_date,
            )
        else:
            assert operation.supersedes is not None  # schema-guaranteed
            fact = store.supersede_fact(
                conn, operation.supersedes,
                category=operation.category,
                statement=operation.statement,
                source_type=operation.source_type,
                source_ref=operation.source_ref,
                confidence=confidence,
                effective_date=operation.effective_date,
            )
        result.applied.append(fact.id)

    conn.execute(
        "UPDATE runs SET finished_at = now(), status = 'succeeded',"
        " actions = %s WHERE id = %s",
        (Jsonb({
            "applied": len(result.applied),
            "rejected": [
                {"index": rejected.index, "reason_code": rejected.reason_code}
                for rejected in result.rejected
            ],
            "uncertainties": result.uncertainties,
        }), run_id),
    )
    conn.commit()
    return result
