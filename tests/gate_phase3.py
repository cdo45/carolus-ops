"""PHASE 3 GATE — DB-backed; no live LLM, no QBO sandbox needed.

Usage:
    CAROLUS_TEST_DB=postgresql://... uv run python -m tests.gate_phase3

DESTRUCTIVE: drops and rebuilds the scratch database's public schema
(refuses to run against DATABASE_URL). The "model" is 25 canned
extraction outputs over 25 seeded construction-client emails, including
3 deliberate violations (fake source_ref, near-duplicate, over-length).

Checks:
  (a/b) every violation rejected with the correct reason code; ZERO
        facts with dangling source_refs (SQL join proof); >= 90% of
        valid operations applied
  (c)   supersede flow: chain integrity, retired fact never renders
  (d)   deterministic rendering: byte-identical re-render; after one new
        fact the diff is confined to its section (+ header count)
  (e)   PASS/FAIL summary, exit 0 only on full pass
"""

from __future__ import annotations

import json
import os
import sys
from difflib import unified_diff
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from db.migrate import migrate  # noqa: E402
from knowledge.extract import process_extraction_output  # noqa: E402
from knowledge.render import render_context  # noqa: E402
from knowledge.store import get_active_facts, get_fact_history  # noqa: E402

# (category, statement, effective_date) per clean email; index = email no.
CLEAN_FACTS: dict[int, tuple[str, str, str | None]] = {
    1: ("entity_profile", "Licensed C-10 electrical contractor in Nevada", None),
    2: ("operations",
        "Crews work four ten-hour days Monday through Thursday", "2026-03-01"),
    3: ("operations",
        "Foreman submits receipt photos at the end of each week", None),
    4: ("accounting_policy",
        "Holds ten percent retainage on all county contracts", None),
    5: ("relationships",
        "General contractor partner is Hadley Construction Group", None),
    6: ("preferences", "Owner wants a call before any filing is submitted", None),
    7: ("watch_items", "Workers comp audit scheduled for the fall", None),
    9: ("entity_profile",
        "Field staff of fourteen including three apprentices", None),
    10: ("accounting_policy",
         "Equipment purchases above five thousand get financed", None),
    11: ("operations",
         "Materials ordered through a single supplier account at Ferguson",
         None),
    12: ("relationships",
         "Bonding agent is Western Surety with a two million limit", None),
    13: ("preferences", "Monthly financial review happens the first Tuesday",
         None),
    14: ("watch_items",
         "Sales tax registration in Arizona may be required next year", None),
    16: ("operations", "Service division bills time and materials weekly",
         None),
    17: ("accounting_policy", "Standard payment terms to subs are net thirty",
         None),
    18: ("entity_profile",
         "Two business checking accounts at First Interstate", None),
    19: ("relationships", "Payroll handled in-house by the office manager",
         None),
    20: ("preferences", "Prefers email over phone for routine questions", None),
    22: ("watch_items", "Old backhoe loan matures in December", None),
    23: ("operations",
         "Winter season shifts work toward indoor tenant improvements", None),
    24: ("accounting_policy",
         "Job deposits collected at twenty five percent for residential",
         None),
    25: ("entity_profile", "Shop and yard leased on a five-year term",
         "2024-07-01"),
}
VIOLATIONS: dict[int, str] = {
    8: "source_not_found",  # op cites an email that does not exist
    15: "duplicate_fact",  # near-copy of email 3's applied fact
    21: "schema_invalid",  # statement blows the 200-char limit
}
TOTAL_EMAILS = 25


def msg_id(index: int) -> str:
    return f"gate-msg-{index:02d}"


def op(index: int, category: str, statement: str,
       effective_date: str | None) -> dict[str, Any]:
    return {
        "op": "add", "category": category, "statement": statement,
        "source_type": "email", "source_ref": msg_id(index),
        "confidence": "stated", "effective_date": effective_date,
        "supersedes": None,
    }


def canned_output(index: int) -> str:
    if index == 8:
        operations = [op(8, "accounting_policy",
                         "Pays subcontractors on net forty-five terms", None)
                      | {"source_ref": "gate-msg-GHOST"}]
    elif index == 15:
        operations = [op(15, "operations",
                         "Foreman submits receipt photos at the end of every"
                         " week", None)]
    elif index == 21:
        operations = [op(21, "watch_items", "x" * 220, None)]
    else:
        category, statement, effective = CLEAN_FACTS[index]
        operations = [op(index, category, statement, effective)]
    return json.dumps({"operations": operations, "uncertainties": []})


def seed_emails(conn: psycopg.Connection, client_id: UUID) -> None:
    for index in range(1, TOTAL_EMAILS + 1):
        conn.execute(
            """
            INSERT INTO emails (client_id, direction, message_id, from_addr,
                                subject)
            VALUES (%s, 'inbound', %s, 'owner@gatethree.example',
                    %s)
            """,
            (client_id, msg_id(index), f"site update week {index}"),
        )
    conn.commit()


def reset_database(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    migrate(url)


def main() -> int:
    load_dotenv()
    url = os.environ.get("CAROLUS_TEST_DB")
    if not url:
        print("CAROLUS_TEST_DB is not set (scratch database required)",
              file=sys.stderr)
        return 2
    if url == os.environ.get("DATABASE_URL"):
        print("REFUSING: CAROLUS_TEST_DB equals DATABASE_URL — the gate"
              " resets its schema", file=sys.stderr)
        return 2

    print("PHASE 3 GATE — scratch database")
    reset_database(url)
    results: list[tuple[str, bool, str]] = []

    with psycopg.connect(url) as conn:
        row = conn.execute(
            "INSERT INTO clients (name, qbo_realm_id) VALUES"
            " ('Gate Three Constructors', 'gate-p3') RETURNING id"
        ).fetchone()
        assert row is not None
        client_id: UUID = row[0]
        conn.commit()
        seed_emails(conn, client_id)

        # (a)+(b) process all 25 canned outputs
        rejections: dict[int, list[str]] = {}
        applied_total = 0
        for index in range(1, TOTAL_EMAILS + 1):
            outcome = process_extraction_output(
                conn, client_id, canned_output(index)
            )
            assert outcome.needs_retry is False
            applied_total += len(outcome.applied)
            if outcome.rejected:
                rejections[index] = [r.reason_code for r in outcome.rejected]

        violation_hits = {
            index: rejections.get(index) == [code]
            for index, code in VIOLATIONS.items()
        }
        ok = all(violation_hits.values()) and set(rejections) == set(VIOLATIONS)
        results.append((
            "violations rejected with correct codes", ok,
            ", ".join(f"#{i}:{rejections.get(i, ['MISSED'])[0]}"
                      for i in sorted(VIOLATIONS)),
        ))

        dangling = conn.execute(
            """
            SELECT count(*) FROM facts f
            WHERE f.source_type = 'email'
              AND NOT EXISTS (
                  SELECT 1 FROM emails e
                  WHERE e.client_id = f.client_id
                    AND e.message_id = f.source_ref
              )
            """,
        ).fetchone()
        assert dangling is not None
        results.append((
            "zero dangling source_refs (join proof)", dangling[0] == 0,
            f"{dangling[0]} dangling",
        ))

        valid_ops = len(CLEAN_FACTS)
        ratio = applied_total / valid_ops
        results.append((
            ">= 90% of valid ops applied", ratio >= 0.90,
            f"{applied_total}/{valid_ops} = {ratio:.0%}",
        ))

        # (c) supersede flow against email 3's fact
        target = next(
            fact for fact in get_active_facts(conn, client_id)
            if fact.source_ref == msg_id(3)
        )
        supersede_raw = json.dumps({"operations": [{
            "op": "supersede", "category": "operations",
            "statement": "Foreman uploads receipt photos directly into the"
                         " portal app",
            "source_type": "email", "source_ref": msg_id(16),
            "confidence": "stated", "effective_date": None,
            "supersedes": str(target.id),
        }], "uncertainties": []})
        supersede_result = process_extraction_output(
            conn, client_id, supersede_raw
        )
        chain = get_fact_history(conn, target.id)
        rendered = render_context(conn, client_id)
        chain_ok = (
            len(supersede_result.applied) == 1
            and [fact.id for fact in chain]
            == [target.id, supersede_result.applied[0]]
            and chain[0].status == "superseded"
            and chain[0].superseded_by == chain[1].id
            and chain[1].status == "active"
        )
        render_ok = (target.statement not in rendered
                     and chain[1].statement in rendered)
        results.append((
            "supersede chain intact, retired fact never renders",
            chain_ok and render_ok,
            f"chain={['%s' % f.status for f in chain]}",
        ))

        # (d) deterministic rendering
        first = render_context(conn, client_id)
        second = render_context(conn, client_id)
        identical = first == second
        new_fact_raw = json.dumps({"operations": [op(
            7, "watch_items",
            "Bid pipeline includes two school district projects for August",
            None,
        )], "uncertainties": []})
        added = process_extraction_output(conn, client_id, new_fact_raw)
        after = render_context(conn, client_id)
        changed = [line[1:] for line in unified_diff(
            first.splitlines(), after.splitlines(), lineterm="", n=0,
        ) if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        confined = bool(changed) and all(
            "active fact" in line
            or "Bid pipeline includes two school district" in line
            or line == "_No facts recorded._"
            for line in changed
        )
        results.append((
            "byte-identical re-render; new fact changes only its section",
            identical and len(added.applied) == 1 and confined,
            f"identical={identical}, changed_lines={len(changed)}",
        ))

    print()
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    verdict = "PASS" if passed == len(results) else "FAIL"
    print(f"\nGATE: {verdict} ({passed}/{len(results)})")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
