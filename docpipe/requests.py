"""The client request list: what we need from the client, computed.

Usage:
    uv run python -m docpipe.requests --realm <realm_id> --period 2026-05

Two sections, exactly what the future portal Requests page reads:
  1. undocumented spend — Purchases/Bills >= $75 in the period hitting
     expense/COGS accounts whose doc_status is still 'unbacked'
  2. unmatched documents — receipts/invoices we hold that match nothing
     (no_matching_txn) or too many things (ambiguous_match)
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv

from rules.close_checklist import parse_period

REQUEST_MIN_AMOUNT: Decimal = Decimal("75")
_EXPENSE_TYPES = ("Expense", "Other Expense", "Cost of Goods Sold")
_UNMATCHED_REASONS = ("no_matching_txn", "ambiguous_match")


@dataclass(frozen=True)
class RequestList:
    period_start: date
    period_end: date
    undocumented_txns: list[dict[str, Any]] = field(default_factory=list)
    unmatched_documents: list[dict[str, Any]] = field(default_factory=list)


def generate_request_list(
    conn: psycopg.Connection, client_id: UUID,
    period_start: date, period_end: date,
) -> RequestList:
    txns = conn.execute(
        """
        SELECT t.txn_type, t.qbo_id, t.txn_date, t.amount,
               COALESCE(e.name, '(no vendor)') AS vendor,
               array_agg(DISTINCT a.name ORDER BY a.name) AS accounts
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        JOIN journal_lines jl ON jl.transaction_id = t.id
        JOIN accounts a ON a.id = jl.account_id
        WHERE t.client_id = %s
          AND t.txn_type IN ('Purchase', 'Bill')
          AND t.txn_date BETWEEN %s AND %s
          AND t.amount >= %s
          AND t.doc_status = 'unbacked'
          AND t.qbo_deleted_at IS NULL
          AND a.acct_type = ANY(%s)
        GROUP BY t.id, e.name
        ORDER BY t.txn_date, t.qbo_id
        """,
        (client_id, period_start, period_end, REQUEST_MIN_AMOUNT,
         list(_EXPENSE_TYPES)),
    ).fetchall()
    documents = conn.execute(
        """
        SELECT filename, doc_type, escalation_reason,
               extracted ->> 'amount' AS amount,
               extracted ->> 'vendor' AS vendor
        FROM documents
        WHERE client_id = %s AND status = 'escalated'
          AND escalation_reason = ANY(%s)
        ORDER BY received_at, id
        """,
        (client_id, list(_UNMATCHED_REASONS)),
    ).fetchall()
    return RequestList(
        period_start=period_start,
        period_end=period_end,
        undocumented_txns=[
            {"txn_type": txn_type, "qbo_id": qbo_id,
             "txn_date": txn_date.isoformat(), "amount": str(amount),
             "vendor": vendor, "accounts": accounts}
            for txn_type, qbo_id, txn_date, amount, vendor, accounts in txns
        ],
        unmatched_documents=[
            {"filename": filename, "doc_type": doc_type, "reason": reason,
             "amount": amount, "vendor": vendor}
            for filename, doc_type, reason, amount, vendor in documents
        ],
    )


def format_request_list(name: str, requests: RequestList) -> str:
    lines = [
        f"Request list — {name}"
        f" ({requests.period_start} to {requests.period_end})",
        "",
        f"Missing documentation ({len(requests.undocumented_txns)}):",
    ]
    if not requests.undocumented_txns:
        lines.append("  (none)")
    for txn in requests.undocumented_txns:
        lines.append(
            f"  {txn['txn_date']}  {txn['txn_type']} {txn['qbo_id']}"
            f"  ${txn['amount']}  {txn['vendor']}"
            f"  [{', '.join(txn['accounts'])}]"
        )
    lines += ["", f"Documents we cannot place ({len(requests.unmatched_documents)}):"]
    if not requests.unmatched_documents:
        lines.append("  (none)")
    for document in requests.unmatched_documents:
        descriptor = document["vendor"] or document["doc_type"]
        amount = f" ${document['amount']}" if document["amount"] else ""
        lines.append(
            f"  {document['filename']}  {descriptor}{amount}"
            f"  ({document['reason']})"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Client document request list")
    parser.add_argument("--realm", required=True, help="QBO realm id")
    parser.add_argument("--period", default=date.today().strftime("%Y-%m"),
                        help="month YYYY-MM")
    args = parser.parse_args(argv)
    period_start, period_end = parse_period(args.period)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id, name FROM clients WHERE qbo_realm_id = %s",
            (args.realm,),
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 1
        client_id, name = row
        requests = generate_request_list(conn, client_id,
                                         period_start, period_end)
    print(format_request_list(name, requests), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
