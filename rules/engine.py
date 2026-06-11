"""Deterministic rules engine with an idempotent flag lifecycle.

Usage:
    uv run python -m rules.engine --realm <realm_id> [--as-of YYYY-MM-DD]

Flag natural key: (client_id, rule_code, source_ref). Per engine run:

  - finding fires, no open flag, key never manually closed -> INSERT open
  - finding fires, open flag exists                        -> untouched
  - open flag exists, finding no longer fires              -> auto-resolve
    with resolution_note 'condition cleared on <as_of>'
  - key was manually dismissed, or manually resolved (any resolution_note
    not written by auto-resolve)                           -> NEVER reopened
  - auto-resolved keys MAY fire again later: that is a new occurrence and
    gets a fresh open flag

Re-running the engine against unchanged data writes zero rows. The engine
only manages rule_codes present in its registry — flags written by sync
(transform_warning, qbo_deleted) keep their own lifecycle. Pure SQL and
Python over canonical tables; no LLM calls anywhere (principle 1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from rules import registry
from rules.base import Finding, Rule, validate_finding, validate_rule

AUTO_RESOLVE_PREFIX: str = "condition cleared on "


@dataclass
class RuleStats:
    findings: int = 0
    new: int = 0
    unchanged: int = 0
    resolved: int = 0
    suppressed: int = 0  # manually closed keys we refused to reopen


def _reconcile(
    conn: psycopg.Connection,
    client_id: UUID,
    rule: Rule,
    findings: list[Finding],
    as_of: date,
) -> RuleStats:
    """Apply the flag lifecycle for one rule's findings."""
    stats = RuleStats()
    open_refs: set[str] = set()
    manually_closed: set[str] = set()
    for source_ref, status, note in conn.execute(
        """
        SELECT source_ref, status, COALESCE(resolution_note, '')
        FROM flags WHERE client_id = %s AND rule_code = %s
        """,
        (client_id, rule.rule_code),
    ).fetchall():
        if status == "open":
            open_refs.add(source_ref)
        elif status == "dismissed":
            manually_closed.add(source_ref)
        elif status == "resolved" and not note.startswith(AUTO_RESOLVE_PREFIX):
            manually_closed.add(source_ref)

    firing: dict[str, Finding] = {}
    for finding in findings:
        validate_finding(rule.rule_code, finding)
        firing.setdefault(finding.source_ref, finding)
    stats.findings = len(firing)

    for source_ref, finding in firing.items():
        if source_ref in open_refs:
            stats.unchanged += 1
        elif source_ref in manually_closed:
            stats.suppressed += 1
        else:
            conn.execute(
                """
                INSERT INTO flags (client_id, rule_code, severity, status,
                                   source_type, source_ref, detail)
                VALUES (%s, %s, %s, 'open', %s, %s, %s)
                """,
                (
                    client_id,
                    rule.rule_code,
                    finding.severity or rule.severity,
                    finding.source_type,
                    source_ref,
                    json.dumps(finding.detail, sort_keys=True, default=str),
                ),
            )
            stats.new += 1

    for source_ref in sorted(open_refs - firing.keys()):
        conn.execute(
            """
            UPDATE flags
            SET status = 'resolved', resolution_note = %s, resolved_at = now()
            WHERE client_id = %s AND rule_code = %s AND source_ref = %s
              AND status = 'open'
            """,
            (
                f"{AUTO_RESOLVE_PREFIX}{as_of.isoformat()}",
                client_id,
                rule.rule_code,
                source_ref,
            ),
        )
        stats.resolved += 1
    return stats


def run_rules(
    conn: psycopg.Connection,
    client_id: UUID,
    *,
    as_of: date | None = None,
    rules: tuple[Rule, ...] | list[Rule] | None = None,
) -> dict[str, Any]:
    """Run all (or the given) rules for one client inside one runs row."""
    as_of = as_of or date.today()
    selected: list[Rule] = list(rules) if rules is not None else list(registry.ALL_RULES)
    for rule in selected:
        validate_rule(rule)

    run_row = conn.execute(
        "INSERT INTO runs (client_id, routine) VALUES (%s, 'rules_engine')"
        " RETURNING id",
        (client_id,),
    ).fetchone()
    assert run_row is not None
    run_id: UUID = run_row[0]
    conn.commit()

    try:
        per_rule: dict[str, dict[str, int]] = {}
        for rule in selected:
            findings = rule.run(conn, client_id, as_of)
            stats = _reconcile(conn, client_id, rule, findings, as_of)
            per_rule[rule.rule_code] = asdict(stats)
        totals = {
            key: sum(stats[key] for stats in per_rule.values())
            for key in ("findings", "new", "unchanged", "resolved", "suppressed")
        }
        summary: dict[str, Any] = {
            "as_of": as_of.isoformat(),
            "rules": per_rule,
            "totals": totals,
        }
        conn.execute(
            "UPDATE runs SET finished_at = now(), status = 'succeeded',"
            " actions = %s WHERE id = %s",
            (Jsonb(summary), run_id),
        )
        conn.commit()
        return summary
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE runs SET finished_at = now(), status = 'failed',"
            " actions = %s WHERE id = %s",
            (Jsonb({"error": type(exc).__name__}), run_id),
        )
        conn.commit()
        raise


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the deterministic rules engine")
    parser.add_argument("--realm", required=True, help="QBO realm id")
    parser.add_argument(
        "--as-of", default=None, help="evaluation date YYYY-MM-DD (default today)"
    )
    args = parser.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id, name FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 1
        client_id, name = row
        summary = run_rules(conn, client_id, as_of=as_of)

    print(f"rules engine — {name} (as of {summary['as_of']})")
    header = f"{'rule':<8}{'findings':>9}{'new':>6}{'unchanged':>11}{'resolved':>10}{'suppressed':>12}"
    print(header)
    for code, stats in summary["rules"].items():
        print(
            f"{code:<8}{stats['findings']:>9}{stats['new']:>6}"
            f"{stats['unchanged']:>11}{stats['resolved']:>10}{stats['suppressed']:>12}"
        )
    totals = summary["totals"]
    print(
        f"{'TOTAL':<8}{totals['findings']:>9}{totals['new']:>6}"
        f"{totals['unchanged']:>11}{totals['resolved']:>10}{totals['suppressed']:>12}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
