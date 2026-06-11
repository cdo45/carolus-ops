"""Bank reconciliation verifier: statement lines vs canonical activity.

rec() matches a VALIDATED statement's lines against journal lines on the
bank account (exact amount + direction, txn_date within ±3 days; a fuzzy
second pass — same amount, up to ±15 days — only ANNOTATES, it never
matches). Direction map: a statement credit (money in) is a debit on the
bank account in the books, and vice versa.

What falls out is the fraud/error surface:
  - statement lines with no QBO transaction  -> flag R040
    (unrecorded bank activity: money moved that the books don't know;
    severity critical). One flag per line, deduped on detail.
  - QBO activity absent from the statement and older than 30 days at
    period end -> flag R041 (uncleared aging; stale checks, phantom
    entries; severity warn).
  - everything unmatched on the QBO side lists as outstanding items
    with ages — normal rec output, not necessarily wrong.

tied = statement movement fully explained by matched QBO activity
(cleared balance equals the ending balance AND zero unmatched statement
lines). Tied recs feed the close checklist's documents_reviewed
condition (see rules/close_checklist.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

EXACT_WINDOW_DAYS: int = 3
FUZZY_WINDOW_DAYS: int = 15
UNCLEARED_AGE_DAYS: int = 30

R040 = "R040"  # unrecorded bank activity (critical)
R041 = "R041"  # uncleared aging (warn)

# statement direction -> required journal posting side on the bank account
_SIDE = {"credit": "debit", "debit": "credit"}


@dataclass(frozen=True)
class _JournalCandidate:
    journal_line_id: UUID
    transaction_id: UUID
    qbo_id: str
    txn_type: str
    txn_date: date
    amount: Decimal
    posting_type: str


@dataclass
class RecResult:
    rec_run_id: UUID
    tied: bool
    matched_count: int
    unmatched_statement_lines: list[dict[str, Any]]
    unmatched_qbo_txns: list[dict[str, Any]]
    outstanding_items: list[dict[str, Any]]
    flags_created: int


def _load_statement(
    conn: psycopg.Connection, client_id: UUID, statement_doc_id: UUID
) -> tuple[dict[str, Any], date, date]:
    row = conn.execute(
        """
        SELECT extracted, status, doc_type, client_id
        FROM documents WHERE id = %s
        """,
        (statement_doc_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"document {statement_doc_id} does not exist")
    extracted, status, doc_type, doc_client = row
    if doc_client != client_id:
        raise ValueError("statement belongs to another client")
    if status != "validated" or doc_type != "bank_statement" or not extracted:
        raise ValueError(
            f"rec needs a VALIDATED bank_statement, got ({doc_type}, {status})"
        )
    return (
        extracted,
        date.fromisoformat(extracted["period_start"]),
        date.fromisoformat(extracted["period_end"]),
    )


def _candidates(
    conn: psycopg.Connection, client_id: UUID, account_id: UUID,
    period_start: date, period_end: date,
) -> list[_JournalCandidate]:
    rows = conn.execute(
        """
        SELECT jl.id, t.id, t.qbo_id, t.txn_type, t.txn_date, jl.amount,
               jl.posting_type
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s
          AND jl.account_id = %s
          AND t.qbo_deleted_at IS NULL
          AND t.txn_date BETWEEN %s AND %s
        ORDER BY t.txn_date, t.qbo_id, jl.line_no
        """,
        (client_id, account_id,
         period_start - timedelta(days=EXACT_WINDOW_DAYS),
         period_end + timedelta(days=EXACT_WINDOW_DAYS)),
    ).fetchall()
    return [_JournalCandidate(*row) for row in rows]


def _flag_once_with_detail(
    conn: psycopg.Connection, client_id: UUID, *, rule_code: str,
    severity: str, source_type: str, source_ref: str, detail: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status,
                           source_type, source_ref, detail)
        SELECT %(client_id)s, %(rule_code)s, %(severity)s, 'open',
               %(source_type)s, %(source_ref)s, %(detail)s
        WHERE NOT EXISTS (
            SELECT 1 FROM flags
            WHERE client_id = %(client_id)s AND rule_code = %(rule_code)s
              AND source_ref = %(source_ref)s AND detail = %(detail)s
              AND status = 'open'
        )
        """,
        {"client_id": client_id, "rule_code": rule_code, "severity": severity,
         "source_type": source_type, "source_ref": source_ref,
         "detail": detail},
    )
    return cur.rowcount


def rec(
    conn: psycopg.Connection, client_id: UUID, account_id: UUID,
    statement_doc_id: UUID,
) -> RecResult:
    """Reconcile one validated statement against one bank account."""
    extracted, period_start, period_end = _load_statement(
        conn, client_id, statement_doc_id
    )
    beginning = Decimal(extracted["beginning_balance"])
    ending = Decimal(extracted["ending_balance"])
    lines = extracted["lines"]
    candidates = _candidates(conn, client_id, account_id,
                             period_start, period_end)

    used: set[UUID] = set()
    matched_count = 0
    cleared = beginning
    unmatched_lines: list[dict[str, Any]] = []

    for line in lines:
        line_date = date.fromisoformat(line["date"])
        amount = Decimal(line["amount"])
        needed_side = _SIDE[line["direction"]]
        exact = [
            candidate for candidate in candidates
            if candidate.journal_line_id not in used
            and candidate.amount == amount
            and candidate.posting_type == needed_side
            and abs((candidate.txn_date - line_date).days) <= EXACT_WINDOW_DAYS
        ]
        if exact:
            best = min(exact, key=lambda c: (abs((c.txn_date - line_date).days),
                                             c.txn_date, c.qbo_id))
            used.add(best.journal_line_id)
            matched_count += 1
            cleared += amount if line["direction"] == "credit" else -amount
            continue

        # fuzzy second pass: annotate only — NEVER counts as a match
        fuzzy = [
            candidate for candidate in candidates
            if candidate.journal_line_id not in used
            and candidate.amount == amount
            and candidate.posting_type == needed_side
            and abs((candidate.txn_date - line_date).days) <= FUZZY_WINDOW_DAYS
        ]
        entry = dict(line)
        if fuzzy:
            nearest = min(fuzzy, key=lambda c: (abs((c.txn_date - line_date).days),
                                                c.txn_date, c.qbo_id))
            entry["fuzzy_candidate"] = {
                "txn_type": nearest.txn_type, "qbo_id": nearest.qbo_id,
                "txn_date": nearest.txn_date.isoformat(),
                "days_apart": abs((nearest.txn_date - line_date).days),
            }
        unmatched_lines.append(entry)

    flags_created = 0
    for entry in unmatched_lines:
        flags_created += _flag_once_with_detail(
            conn, client_id, rule_code=R040, severity="critical",
            source_type="document", source_ref=str(statement_doc_id),
            detail=json.dumps(entry, sort_keys=True),
        )

    unmatched_qbo: list[dict[str, Any]] = []
    outstanding: list[dict[str, Any]] = []
    aged_txn_ids: set[UUID] = set()
    for candidate in candidates:
        if candidate.journal_line_id in used:
            continue
        age_days = (period_end - candidate.txn_date).days
        item = {
            "txn_type": candidate.txn_type, "qbo_id": candidate.qbo_id,
            "txn_date": candidate.txn_date.isoformat(),
            "amount": str(candidate.amount), "side": candidate.posting_type,
            "age_days": age_days,
        }
        unmatched_qbo.append(item)
        outstanding.append(item)
        if age_days > UNCLEARED_AGE_DAYS and candidate.transaction_id not in aged_txn_ids:
            aged_txn_ids.add(candidate.transaction_id)
            flags_created += _flag_once_with_detail(
                conn, client_id, rule_code=R041, severity="warn",
                source_type="transaction",
                source_ref=str(candidate.transaction_id),
                detail=json.dumps(item, sort_keys=True),
            )

    tied = cleared == ending and not unmatched_lines

    row = conn.execute(
        """
        INSERT INTO rec_runs (client_id, account_id, statement_doc_id,
                              period_start, period_end, statement_beginning,
                              statement_ending, qbo_cleared_balance,
                              matched_count, unmatched_statement_lines,
                              unmatched_qbo_txns, outstanding_items, tied)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, account_id, statement_doc_id) DO UPDATE SET
            period_start = excluded.period_start,
            period_end = excluded.period_end,
            statement_beginning = excluded.statement_beginning,
            statement_ending = excluded.statement_ending,
            qbo_cleared_balance = excluded.qbo_cleared_balance,
            matched_count = excluded.matched_count,
            unmatched_statement_lines = excluded.unmatched_statement_lines,
            unmatched_qbo_txns = excluded.unmatched_qbo_txns,
            outstanding_items = excluded.outstanding_items,
            tied = excluded.tied
        WHERE (rec_runs.period_start, rec_runs.period_end,
               rec_runs.statement_beginning, rec_runs.statement_ending,
               rec_runs.qbo_cleared_balance, rec_runs.matched_count,
               rec_runs.unmatched_statement_lines, rec_runs.unmatched_qbo_txns,
               rec_runs.outstanding_items, rec_runs.tied)
            IS DISTINCT FROM
            (excluded.period_start, excluded.period_end,
             excluded.statement_beginning, excluded.statement_ending,
             excluded.qbo_cleared_balance, excluded.matched_count,
             excluded.unmatched_statement_lines, excluded.unmatched_qbo_txns,
             excluded.outstanding_items, excluded.tied)
        RETURNING id
        """,
        (client_id, account_id, statement_doc_id, period_start, period_end,
         beginning, ending, cleared, matched_count,
         Jsonb(unmatched_lines), Jsonb(unmatched_qbo), Jsonb(outstanding),
         tied),
    ).fetchone()
    if row is None:  # unchanged re-run
        row = conn.execute(
            "SELECT id FROM rec_runs WHERE client_id = %s AND account_id = %s"
            " AND statement_doc_id = %s",
            (client_id, account_id, statement_doc_id),
        ).fetchone()
    assert row is not None
    conn.commit()
    return RecResult(
        rec_run_id=row[0], tied=tied, matched_count=matched_count,
        unmatched_statement_lines=unmatched_lines,
        unmatched_qbo_txns=unmatched_qbo, outstanding_items=outstanding,
        flags_created=flags_created,
    )
