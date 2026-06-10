"""Extraction harness tests with CANNED model outputs — no live LLM.

Covers: payload assembly, clean apply, hallucinated source_ref rejection,
near-duplicate rejection, malformed JSON -> needs_retry, fence stripping,
and runs-row logging on both paths."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg
import pytest

from knowledge.extract import (
    PROMPT_VERSION,
    SourceItem,
    assemble_extraction_input,
    process_extraction_output,
    strip_fences,
)
from knowledge.store import add_fact, get_active_facts
from tests.conftest import make_client


@pytest.fixture
def client_id(conn: psycopg.Connection) -> UUID:
    return make_client(conn)


def seed_email(conn: psycopg.Connection, client_id: UUID, message_id: str) -> None:
    conn.execute(
        "INSERT INTO emails (client_id, direction, message_id, subject)"
        " VALUES (%s, 'inbound', %s, 'site update')",
        (client_id, message_id),
    )
    conn.commit()


def model_output(*ops: dict[str, Any], uncertainties: list[str] | None = None,
                 fenced: bool = False) -> str:
    body = json.dumps(
        {"operations": list(ops), "uncertainties": uncertainties or []}
    )
    return f"```json\n{body}\n```" if fenced else body


def email_op(message_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "op": "add",
        "category": "operations",
        "statement": "Foreman submits receipts by photo on Fridays",
        "source_type": "email",
        "source_ref": message_id,
        "confidence": "stated",
        "effective_date": None,
        "supersedes": None,
    }
    base.update(overrides)
    return base


def runs_rows(conn: psycopg.Connection, client_id: UUID) -> list[tuple]:
    return conn.execute(
        "SELECT status, actions FROM runs WHERE client_id = %s"
        " AND routine = 'fact_extraction' ORDER BY started_at",
        (client_id,),
    ).fetchall()


# ------------------------------------------------------------ assembly


def test_assemble_extraction_input(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    add_fact(conn, client_id, category="operations",
             statement="Runs two crews; second crew is subbed labor",
             source_type="carlos", source_ref="carlos:n1")
    conn.commit()
    source = SourceItem(source_type="email", source_ref="msg-7",
                        content="We moved to 4x10s starting March 1.")

    payload = assemble_extraction_input(conn, client_id, source)

    assert payload["prompt_version"] == PROMPT_VERSION
    assert "## Output contract" in payload["system"], "prompt file included"
    (fact,) = payload["input"]["active_facts"]
    assert fact["category"] == "operations"
    assert UUID(fact["id"]), "fact ids included so the model can supersede"
    assert payload["input"]["source"] == {
        "source_type": "email", "source_ref": "msg-7",
        "content": "We moved to 4x10s starting March 1.",
    }


def test_strip_fences() -> None:
    assert strip_fences('{"a": 1}') == '{"a": 1}'
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('  ```json\n{"a": 1}\n```  ') == '{"a": 1}'


# ------------------------------------------------------------ apply paths


def test_clean_batch_applies(conn: psycopg.Connection, client_id: UUID) -> None:
    seed_email(conn, client_id, "msg-1")
    raw = model_output(
        email_op("msg-1"),
        email_op("msg-1",
                 category="accounting_policy",
                 statement="Holds ten percent retainage on county work",
                 confidence="inferred", effective_date="2026-01-15"),
        uncertainties=["unclear whether the new excavator is leased"],
        fenced=True,
    )

    result = process_extraction_output(conn, client_id, raw)

    assert result.needs_retry is False
    assert len(result.applied) == 2 and result.rejected == []
    assert result.uncertainties == ["unclear whether the new excavator is leased"]
    facts = get_active_facts(conn, client_id)
    assert {fact.statement for fact in facts} == {
        "Foreman submits receipts by photo on Fridays",
        "Holds ten percent retainage on county work",
    }
    by_statement = {fact.statement: fact for fact in facts}
    retainage = by_statement["Holds ten percent retainage on county work"]
    assert str(retainage.confidence) == "0.50", "inferred -> 0.50"
    assert str(retainage.effective_date) == "2026-01-15"
    ((status, actions),) = runs_rows(conn, client_id)
    assert status == "succeeded"
    assert actions["applied"] == 2 and actions["rejected"] == []


def test_hallucinated_source_ref_rejected_and_logged(
    conn: psycopg.Connection, client_id: UUID
) -> None:
    seed_email(conn, client_id, "msg-1")
    raw = model_output(
        email_op("msg-1"),
        email_op("msg-GHOST",
                 statement="Pays all subcontractors on net-45 terms"),
    )

    result = process_extraction_output(conn, client_id, raw)

    assert len(result.applied) == 1
    assert [r.reason_code for r in result.rejected] == ["source_not_found"]
    statements = {fact.statement for fact in get_active_facts(conn, client_id)}
    assert "Pays all subcontractors on net-45 terms" not in statements
    ((status, actions),) = runs_rows(conn, client_id)
    assert status == "succeeded"
    assert actions["rejected"] == [{"index": 1, "reason_code": "source_not_found"}]


def test_near_duplicate_rejected(conn: psycopg.Connection, client_id: UUID) -> None:
    seed_email(conn, client_id, "msg-1")
    add_fact(conn, client_id, category="operations",
             statement="Foreman submits receipts by photo on Fridays",
             source_type="carlos", source_ref="carlos:n1")
    conn.commit()

    result = process_extraction_output(
        conn, client_id,
        model_output(email_op("msg-1",
                              statement="Foreman submits receipts by photo"
                                        " on Friday")),
    )

    assert result.applied == []
    assert [r.reason_code for r in result.rejected] == ["duplicate_fact"]


def test_supersede_via_output(conn: psycopg.Connection, client_id: UUID) -> None:
    seed_email(conn, client_id, "msg-2")
    original = add_fact(conn, client_id, category="operations",
                        statement="Runs two crews; second crew is subbed labor",
                        source_type="carlos", source_ref="carlos:n1")
    conn.commit()

    result = process_extraction_output(
        conn, client_id,
        model_output(email_op(
            "msg-2", op="supersede", supersedes=str(original.id),
            statement="Runs three crews; all in-house since January",
        )),
    )

    assert len(result.applied) == 1 and result.rejected == []
    (active,) = get_active_facts(conn, client_id)
    assert active.id == result.applied[0]
    retired = conn.execute(
        "SELECT status, superseded_by FROM facts WHERE id = %s",
        (original.id,),
    ).fetchone()
    assert retired == ("superseded", active.id)


# ------------------------------------------------------------ retry paths


@pytest.mark.parametrize(
    ("raw", "error_match"),
    [
        ("{not json at all", "malformed_json"),
        ('"just a string"', "not a JSON object"),
        ('{"facts": []}', "missing 'operations'"),
        ('{"operations": {}, "uncertainties": []}', "missing 'operations'"),
        ('{"operations": [], "uncertainties": [1]}', "string list"),
    ],
)
def test_bad_output_signals_retry_and_writes_nothing(
    conn: psycopg.Connection, client_id: UUID, raw: str, error_match: str
) -> None:
    result = process_extraction_output(conn, client_id, raw)

    assert result.needs_retry is True
    assert result.error is not None and error_match in result.error
    assert result.applied == []
    assert get_active_facts(conn, client_id) == []
    ((status, actions),) = runs_rows(conn, client_id)
    assert status == "failed"
    assert actions["needs_retry"] is True
