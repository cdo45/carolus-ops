"""Monthly close checklist: named conditions, one summary row per period.

Usage:
    uv run python -m rules.close_checklist --realm <realm_id> [--period 2026-06]

Evaluates, for a client and calendar month:

  no_open_critical_flags   no flags with severity=critical sitting open
  suspense_zeroed          suspense/clearing/uncategorized-named accounts
                           net to zero as of period end
  no_stale_uncategorized   no Uncategorized* lines older than 14 days at
                           period end (same logic as R015)
  documents_reviewed       STUB until Phase 4: always 'not_evaluated' —
                           explicitly marked, never silently passing

Overall: red if anything failed; incomplete if nothing failed but
something is not_evaluated (true until Phase 4 ships); green only when
every condition passes. Storage: close_runs table, one row per
(client, period), change-guarded upsert — re-evaluating unchanged books
writes zero rows and keeps the original evaluated_at (see migration 0006
for why close_runs and not kpi_values).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from rules import r013_suspense_aging, r015_stale_uncategorized

PASS = "pass"
FAIL = "fail"
NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class Condition:
    name: str
    state: str  # pass | fail | not_evaluated
    detail: dict[str, Any]


@dataclass(frozen=True)
class CloseResult:
    period_start: date
    period_end: date
    status: str  # green | red | incomplete
    conditions: list[Condition]

    def as_json(self) -> list[dict[str, Any]]:
        return [
            {"name": c.name, "state": c.state, "detail": c.detail}
            for c in self.conditions
        ]


def parse_period(period: str) -> tuple[date, date]:
    """'2026-06' -> (2026-06-01, 2026-06-30)."""
    year, month = (int(part) for part in period.split("-"))
    start = date(year, month, 1)
    end = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    return start, end


def _no_open_critical_flags(conn: psycopg.Connection, client_id: UUID) -> Condition:
    rows = conn.execute(
        """
        SELECT rule_code, count(*) FROM flags
        WHERE client_id = %s AND severity = 'critical' AND status = 'open'
        GROUP BY rule_code ORDER BY rule_code
        """,
        (client_id,),
    ).fetchall()
    open_by_rule = {code: count for code, count in rows}
    return Condition(
        name="no_open_critical_flags",
        state=PASS if not open_by_rule else FAIL,
        detail={"open_critical_by_rule": open_by_rule},
    )


def _suspense_zeroed(
    conn: psycopg.Connection, client_id: UUID, period_end: date
) -> Condition:
    rows = conn.execute(
        """
        SELECT a.name,
               SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) AS net
        FROM accounts a
        JOIN journal_lines jl ON jl.account_id = a.id
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE a.client_id = %s
          AND a.name ~* %s
          AND t.txn_date <= %s
          AND t.qbo_deleted_at IS NULL
        GROUP BY a.id
        HAVING SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) <> 0
        ORDER BY a.name
        """,
        (client_id, r013_suspense_aging.NAME_PATTERN, period_end),
    ).fetchall()
    nonzero = {name: str(net) for name, net in rows}
    return Condition(
        name="suspense_zeroed",
        state=PASS if not nonzero else FAIL,
        detail={"nonzero_balances": nonzero},
    )


def _no_stale_uncategorized(
    conn: psycopg.Connection, client_id: UUID, period_end: date
) -> Condition:
    findings = r015_stale_uncategorized.run(conn, client_id, period_end)
    return Condition(
        name="no_stale_uncategorized",
        state=PASS if not findings else FAIL,
        detail={"stale_count": len(findings)},
    )


def _documents_reviewed(
    conn: psycopg.Connection, client_id: UUID, period_start: date, period_end: date
) -> Condition:
    rows = conn.execute(
        """
        SELECT doc_status, count(*) FROM transactions
        WHERE client_id = %s AND txn_date BETWEEN %s AND %s
          AND qbo_deleted_at IS NULL
        GROUP BY doc_status ORDER BY doc_status
        """,
        (client_id, period_start, period_end),
    ).fetchall()
    return Condition(
        name="documents_reviewed",
        state=NOT_EVALUATED,
        detail={
            "note": (
                "document pipeline is Phase 4 — condition is explicitly"
                " not evaluated, never silently passing"
            ),
            "doc_status_counts": {status: count for status, count in rows},
        },
    )


def evaluate_close(
    conn: psycopg.Connection, client_id: UUID, period_start: date, period_end: date
) -> CloseResult:
    conditions = [
        _no_open_critical_flags(conn, client_id),
        _suspense_zeroed(conn, client_id, period_end),
        _no_stale_uncategorized(conn, client_id, period_end),
        _documents_reviewed(conn, client_id, period_start, period_end),
    ]
    if any(c.state == FAIL for c in conditions):
        status = "red"
    elif any(c.state == NOT_EVALUATED for c in conditions):
        status = "incomplete"
    else:
        status = "green"
    return CloseResult(period_start, period_end, status, conditions)


def persist_close(
    conn: psycopg.Connection, client_id: UUID, result: CloseResult
) -> int:
    """Upsert the period's close row; identical re-evaluation writes zero."""
    cur = conn.execute(
        """
        INSERT INTO close_runs (client_id, period_start, period_end, status,
                                conditions)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (client_id, period_start, period_end) DO UPDATE SET
            status = excluded.status,
            conditions = excluded.conditions,
            evaluated_at = now()
        WHERE (close_runs.status, close_runs.conditions)
            IS DISTINCT FROM (excluded.status, excluded.conditions)
        """,
        (
            client_id,
            result.period_start,
            result.period_end,
            result.status,
            Jsonb(result.as_json()),
        ),
    )
    conn.commit()
    return cur.rowcount


_STATE_MARK = {PASS: "[PASS]", FAIL: "[FAIL]", NOT_EVALUATED: "[ -- ]"}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Monthly close checklist")
    parser.add_argument("--realm", required=True, help="QBO realm id")
    parser.add_argument(
        "--period", default=date.today().strftime("%Y-%m"), help="month YYYY-MM"
    )
    args = parser.parse_args(argv)
    period_start, period_end = parse_period(args.period)

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
            return 2
        client_id, name = row
        result = evaluate_close(conn, client_id, period_start, period_end)
        persist_close(conn, client_id, result)

    print(f"close checklist — {name}, {args.period}")
    for condition in result.conditions:
        print(f"  {_STATE_MARK[condition.state]} {condition.name}")
        if condition.state == FAIL:
            print(f"         {json.dumps(condition.detail, default=str)}")
    print(f"CLOSE: {result.status.upper()}")
    return 0 if result.status == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
