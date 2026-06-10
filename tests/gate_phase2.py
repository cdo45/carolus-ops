"""PHASE 2 GATE — runs against the LIVE QBO SANDBOX. NOT part of CI.

Prereq: tests/seed_errors.py has planted the 15-violation manifest
(data/seed_manifest.json) in the same sandbox and the same calendar month.

Checks:
  (a) detection — full sync, engine run, then >= 14/15 manifest items must
      carry an OPEN flag with the EXPECTED rule_code on the correct
      canonical row
  (b) provenance — every open engine flag's source_ref resolves to an
      existing canonical row of its source_type (principle 2)
  (c) idempotency — a second engine run creates zero new open flags

Usage:
  uv run python -m tests.gate_phase2 --realm <sandbox_realm_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from rules.engine import run_rules  # noqa: E402
from rules.registry import ALL_RULES  # noqa: E402
from sync.full_sync import run_full_sync  # noqa: E402

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_manifest.json"

_SOURCE_TABLES = {
    "transaction": "transactions",
    "account": "accounts",
    "entity": "entities",
    "job": "jobs",
}


def resolve_target(
    conn: psycopg.Connection, client_id: UUID, item: dict[str, Any]
) -> UUID | None:
    """Map a manifest item's sandbox qbo_id to its canonical row UUID."""
    target = item["target"]
    qbo_id = item["qbo_id"]
    if target == "transaction":
        row = conn.execute(
            "SELECT id FROM transactions WHERE client_id = %s AND qbo_id = %s"
            " AND txn_type = %s",
            (client_id, qbo_id, item["entity_type"]),
        ).fetchone()
    elif target == "account":
        row = conn.execute(
            "SELECT id FROM accounts WHERE client_id = %s AND qbo_id = %s",
            (client_id, qbo_id),
        ).fetchone()
    elif target == "entity":
        row = conn.execute(
            "SELECT id FROM entities WHERE client_id = %s AND qbo_id = %s"
            " AND kind = %s",
            (client_id, qbo_id, item["target_kind"]),
        ).fetchone()
    elif target == "job":
        row = conn.execute(
            "SELECT id FROM jobs WHERE client_id = %s AND qbo_id = %s",
            (client_id, qbo_id),
        ).fetchone()
    else:
        row = None
    return row[0] if row else None


def check_provenance(conn: psycopg.Connection, client_id: UUID) -> list[str]:
    """Every open engine flag must point at an existing canonical row."""
    engine_codes = [rule.rule_code for rule in ALL_RULES]
    problems: list[str] = []
    rows = conn.execute(
        """
        SELECT rule_code, source_type, source_ref FROM flags
        WHERE client_id = %s AND status = 'open' AND rule_code = ANY(%s)
        """,
        (client_id, engine_codes),
    ).fetchall()
    for rule_code, source_type, source_ref in rows:
        table = _SOURCE_TABLES.get(source_type)
        if table is None:
            problems.append(f"{rule_code}: unknown source_type {source_type!r}")
            continue
        exists = conn.execute(  # noqa: S608 - table from fixed mapping above
            f"SELECT 1 FROM {table} WHERE id = %s::uuid AND client_id = %s",
            (source_ref, client_id),
        ).fetchone()
        if exists is None:
            problems.append(
                f"{rule_code}: source_ref {source_ref} not found in {table}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Phase 2 gate (live sandbox)")
    parser.add_argument("--realm", required=True, help="connected sandbox realm id")
    args = parser.parse_args(argv)

    if not MANIFEST_PATH.exists():
        print(f"no manifest at {MANIFEST_PATH} — run tests/seed_errors.py first",
              file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST_PATH.read_text())
    if manifest.get("realm") != args.realm:
        print(f"manifest realm {manifest.get('realm')} != --realm {args.realm}",
              file=sys.stderr)
        return 2
    items: list[dict[str, Any]] = manifest["items"]

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 2
        client_id: UUID = row[0]

        print(f"PHASE 2 GATE — realm {args.realm}")
        print("  syncing sandbox ...")
        run_full_sync(conn, client_id, args.realm)
        print("  running rules engine ...")
        first = run_rules(conn, client_id)

        # (a) detection vs manifest
        hits = 0
        print(f"\n  {'n':>3} {'rule':<6} {'hit':<5} target")
        for item in items:
            target_uuid = resolve_target(conn, client_id, item)
            flagged = False
            if target_uuid is not None:
                flagged = conn.execute(
                    """
                    SELECT 1 FROM flags
                    WHERE client_id = %s AND rule_code = %s
                      AND source_ref = %s AND status = 'open'
                    """,
                    (client_id, item["rule_code"], str(target_uuid)),
                ).fetchone() is not None
            hits += flagged
            mark = "HIT" if flagged else "MISS"
            print(f"  {item['n']:>3} {item['rule_code']:<6} {mark:<5}"
                  f" {item['entity_type']} {item['qbo_id']}"
                  f" ({item['description']})")
        detection_ok = hits >= 14
        print(f"\n  [{'PASS' if detection_ok else 'FAIL'}] detection:"
              f" {hits}/{len(items)} manifest items flagged (need >= 14)")

        # (b) provenance
        problems = check_provenance(conn, client_id)
        provenance_ok = not problems
        print(f"  [{'PASS' if provenance_ok else 'FAIL'}] provenance:"
              f" {len(problems)} dangling source_refs")
        for problem in problems[:10]:
            print(f"      {problem}")

        # (c) idempotent second engine run
        second = run_rules(conn, client_id)
        new_flags = second["totals"]["new"]
        idempotent_ok = new_flags == 0
        print(f"  [{'PASS' if idempotent_ok else 'FAIL'}] idempotency:"
              f" second engine run created {new_flags} new open flags"
              " (must be 0)")

        passed = sum((detection_ok, provenance_ok, idempotent_ok))
        verdict = "PASS" if passed == 3 else "FAIL"
        print(f"\nGATE: {verdict} ({passed}/3)"
              f" — engine totals: {first['totals']}")
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
