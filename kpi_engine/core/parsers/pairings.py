"""QBO "Invoices and Received Payments" / "Bills and Applied Payments" parser.

Sections are customer (AR) or vendor (AP) names; there are no total rows.
The two reports disagree on column order (AP puts Transaction number before
Memo/Description and has no paid-status column), so columns are mapped by
header name, never position.

GROUPING HEURISTIC (group_key): within a party's section, rows are assigned
sequential cluster ids — a new cluster starts at each payment-side row;
non-payment rows (invoices, credits, others) attach to the most recent
payment cluster; invoice-side rows appearing before any payment in the
section each get their own cluster. Real exports mostly order
payment-then-its-invoices but not strictly, so the group_key is a HINT for
the KPI layer's amount-based matching (chunk 6), not truth.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from core.detect import detect_from_rows, read_rows
from core.parsers.common import (
    extract_job_prefix,
    is_footer_row,
    normalize_cell,
    parse_amount,
)

BAD_PAIRINGS_MESSAGE = (
    "This file doesn't look like a QBO Invoices and Received Payments or "
    "Bills and Applied Payments export — re-export the report from QBO."
)

_DATE_LABELS = {"date"}
_TYPE_LABELS = {"transaction type"}
_MEMO_LABELS = {"memo/description", "memo / description", "memo"}
_NUM_LABELS = {"transaction number", "num"}
_AMOUNT_LABELS = {"amount"}
_PAID_LABELS = {"a/r paid", "ar paid"}
_OPEN_LABELS = {"open balance"}


class PairingsParseError(Exception):
    """Raised when a pairings file is structurally unusable."""


@dataclass
class PairingRow:
    txn_date: str | None
    txn_type: str | None
    num: str | None
    memo: str | None
    amount: float | None
    paid_status: str | None  # AR only: "Paid"/"Unpaid"; None on AP
    open_balance: float | None  # None when empty, 0.0 for "0.00"
    row_type: str  # "invoice" | "payment" | "credit" | "other"
    group_key: str
    job_prefix: str | None


@dataclass
class PairingParty:
    name: str
    rows: list[PairingRow] = field(default_factory=list)


@dataclass
class PairingsParseResult:
    period_start: str | None
    period_end: str | None
    side: str  # "AR" | "AP"
    parties: list[PairingParty]
    warnings: list[str]
    excluded_rows: list[dict]
    row_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)


def _parse_date(text: str) -> str | None:
    try:
        return dt.datetime.strptime(text.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def classify_row_type(txn_type: str | None) -> str:
    """Invoice/Bill → invoice-side; any *Payment* → payment-side;
    Vendor Credit → credit; everything else (Journal Entry, Deposit) → other.
    """
    norm = normalize_cell(txn_type or "")
    if norm in ("invoice", "bill"):
        return "invoice"
    if "payment" in norm:
        return "payment"
    if norm == "vendor credit":
        return "credit"
    return "other"


def _map_columns(header: list[str]) -> dict[str, int | None]:
    columns: dict[str, int | None] = {
        "date": None,
        "type": None,
        "memo": None,
        "num": None,
        "amount": None,
        "paid": None,
        "open": None,
    }
    labels = [
        (_DATE_LABELS, "date"),
        (_TYPE_LABELS, "type"),
        (_MEMO_LABELS, "memo"),
        (_NUM_LABELS, "num"),
        (_AMOUNT_LABELS, "amount"),
        (_PAID_LABELS, "paid"),
        (_OPEN_LABELS, "open"),
    ]
    for i, cell in enumerate(header):
        label = normalize_cell(cell)
        for label_set, key in labels:
            if label in label_set and columns[key] is None:
                columns[key] = i
                break
    return columns


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def parse_pairings(filepath: str | Path) -> PairingsParseResult:
    rows = read_rows(filepath)
    detection = detect_from_rows(rows)
    header_idx = detection.header_row_index
    if header_idx is None:
        raise PairingsParseError(BAD_PAIRINGS_MESSAGE)

    columns = _map_columns(rows[header_idx])
    required = (columns["date"], columns["type"], columns["num"],
                columns["amount"])
    if any(c is None for c in required):
        raise PairingsParseError(BAD_PAIRINGS_MESSAGE)

    if detection.report_type == "INVOICES_PAYMENTS":
        side = "AR"
    elif detection.report_type == "BILLS_PAYMENTS":
        side = "AP"
    else:
        # Fall back on the paid-status column, present only on the AR report.
        side = "AR" if columns["paid"] is not None else "AP"

    warnings: list[str] = []
    excluded_rows: list[dict] = []
    parties: list[PairingParty] = []
    counts = {
        "preamble": header_idx,
        "header": 1,
        "party": 0,
        "row": 0,
        "excluded": 0,
    }

    def exclude(row_number: int, row: list[str], reason: str) -> None:
        counts["excluded"] += 1
        excluded_rows.append(
            {"row_number": row_number, "raw": row, "reason": reason}
        )

    current: PairingParty | None = None
    cluster = 0
    seen_payment = False

    for idx in range(header_idx + 1, len(rows)):
        row = rows[idx]
        row_number = idx + 1

        if not any(c.strip() for c in row):
            exclude(row_number, row, "blank row")
            continue
        if is_footer_row(row):
            exclude(row_number, row, "report footer")
            continue

        col_a = row[0].strip() if row else ""
        if col_a:
            counts["party"] += 1
            current = PairingParty(name=col_a)
            parties.append(current)
            cluster = 0
            seen_payment = False
            continue

        date_text = _cell(row, columns["date"])
        iso_date = _parse_date(date_text)
        if iso_date is None:
            raise PairingsParseError(
                f"Row {row_number} couldn't be classified as a transaction "
                f"row, party marker, or footer: {row!r}. Re-export the "
                "report from QBO."
            )
        if current is None:
            raise PairingsParseError(
                f"Row {row_number}: transaction dated {date_text} appeared "
                "outside any customer/vendor section."
            )

        txn_type = _cell(row, columns["type"]) or None
        row_type = classify_row_type(txn_type)
        if row_type == "payment":
            cluster += 1
            seen_payment = True
        elif not seen_payment:
            # Invoice-side rows before any payment: own cluster each.
            cluster += 1
        group_key = f"{current.name}::{cluster}"

        num = _cell(row, columns["num"]) or None
        paid = _cell(row, columns["paid"]) or None
        counts["row"] += 1
        current.rows.append(
            PairingRow(
                txn_date=iso_date,
                txn_type=txn_type,
                num=num,
                memo=_cell(row, columns["memo"]) or None,
                amount=parse_amount(_cell(row, columns["amount"])),
                paid_status=paid if side == "AR" else None,
                open_balance=parse_amount(_cell(row, columns["open"])),
                row_type=row_type,
                group_key=group_key,
                job_prefix=extract_job_prefix(num),
            )
        )

    accounted = sum(counts.values())
    if accounted != len(rows):
        raise PairingsParseError(
            f"Internal row reconciliation failed: {accounted} rows accounted "
            f"for out of {len(rows)} read. This is a parser bug — please "
            "report it."
        )

    return PairingsParseResult(
        period_start=detection.period_start,
        period_end=detection.period_end,
        side=side,
        parties=parties,
        warnings=warnings,
        excluded_rows=excluded_rows,
        row_count=len(rows),
        counts=counts,
    )
