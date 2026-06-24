"""QBO A/R & A/P Aging Detail parser (one parser, two report types).

Both reports share the bucket-section structure ("91 or more days past due"
... "CURRENT", closed by "Total for <bucket>" rows, with a final TOTAL row
carrying the report-level grand total). They differ in one column: A/P
carries a "Past due" day count (negative for not-yet-due rows in CURRENT)
and names its party column "Vendor display name" instead of "Customer full
name". Columns are mapped by header name, not position.

Open-balance cells are EMPTY or "0.00" interchangeably in real exports;
empty parses to None (unknown), "0.00" to 0.0.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from core.detect import detect_from_rows, read_rows
from core.parsers.common import is_footer_row, normalize_cell, parse_amount

BALANCE_TOLERANCE = 0.005

BAD_AGING_MESSAGE = (
    "This file doesn't look like a QBO A/R or A/P Aging Detail export — "
    "re-export using QBO's standard Aging Detail report."
)

_DATE_LABELS = {"date"}
_TYPE_LABELS = {"transaction type"}
_NUM_LABELS = {"num"}
_CUSTOMER_LABELS = {"customer full name", "customer"}
_VENDOR_LABELS = {"vendor display name", "vendor"}
_DUE_LABELS = {"due date"}
_PAST_DUE_LABELS = {"past due"}
_AMOUNT_LABELS = {"amount"}
_OPEN_LABELS = {"open balance"}


class AgingParseError(Exception):
    """Raised when an aging file is structurally unusable."""


@dataclass
class AgingRow:
    txn_date: str | None
    txn_type: str | None
    num: str | None
    party: str | None  # customer (AR) or vendor (AP)
    due_date: str | None
    past_due_days: int | None  # AP only; None on AR
    amount: float | None
    open_balance: float | None  # None when the cell was empty, 0.0 for "0.00"


@dataclass
class AgingBucket:
    name: str
    rows: list[AgingRow] = field(default_factory=list)
    declared_total: float | None = None


@dataclass
class AgingParseResult:
    as_of_date: str | None
    side: str  # "AR" | "AP"
    buckets: list[AgingBucket]
    grand_total: float | None
    warnings: list[str]
    excluded_rows: list[dict]
    row_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)


def _parse_date(text: str) -> str | None:
    try:
        return dt.datetime.strptime(text.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _map_columns(header: list[str]) -> dict[str, int | None]:
    columns: dict[str, int | None] = {
        "date": None,
        "type": None,
        "num": None,
        "customer": None,
        "vendor": None,
        "due": None,
        "past_due": None,
        "amount": None,
        "open": None,
    }
    labels = [
        (_DATE_LABELS, "date"),
        (_TYPE_LABELS, "type"),
        (_NUM_LABELS, "num"),
        (_CUSTOMER_LABELS, "customer"),
        (_VENDOR_LABELS, "vendor"),
        (_DUE_LABELS, "due"),
        (_PAST_DUE_LABELS, "past_due"),
        (_AMOUNT_LABELS, "amount"),
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


def parse_aging(filepath: str | Path) -> AgingParseResult:
    rows = read_rows(filepath)
    detection = detect_from_rows(rows)
    header_idx = detection.header_row_index
    if header_idx is None:
        raise AgingParseError(BAD_AGING_MESSAGE)

    columns = _map_columns(rows[header_idx])
    party_col = (
        columns["customer"] if columns["customer"] is not None
        else columns["vendor"]
    )
    required = (columns["date"], columns["type"], columns["amount"],
                columns["open"], party_col)
    if any(c is None for c in required):
        raise AgingParseError(BAD_AGING_MESSAGE)
    side = "AR" if columns["customer"] is not None else "AP"

    warnings: list[str] = []
    excluded_rows: list[dict] = []
    buckets: list[AgingBucket] = []
    grand_total: float | None = None
    counts = {
        "preamble": header_idx,
        "header": 1,
        "bucket": 0,
        "row": 0,
        "bucket_total": 0,
        "grand_total": 0,
        "excluded": 0,
    }

    def exclude(row_number: int, row: list[str], reason: str) -> None:
        counts["excluded"] += 1
        excluded_rows.append(
            {"row_number": row_number, "raw": row, "reason": reason}
        )

    current: AgingBucket | None = None

    def close_bucket(declared: float | None) -> None:
        nonlocal current
        if current is None:
            return
        current.declared_total = declared
        row_sum = sum(
            r.open_balance for r in current.rows if r.open_balance is not None
        )
        if declared is not None and abs(row_sum - declared) > BALANCE_TOLERANCE:
            warnings.append(
                f"Bucket {current.name!r}: rows sum to {row_sum:.2f} but the "
                f"report's bucket total is {declared:.2f}."
            )
        current = None

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
            norm = normalize_cell(col_a)
            if norm == "total":
                # Report-level grand total, not a bucket.
                counts["grand_total"] += 1
                grand_total = parse_amount(_cell(row, columns["open"]))
                if grand_total is None:
                    grand_total = parse_amount(_cell(row, columns["amount"]))
                if current is not None:
                    warnings.append(
                        f"Bucket {current.name!r} had no Total row before "
                        "the grand TOTAL."
                    )
                    close_bucket(None)
                continue
            if norm.startswith("total for"):
                if current is None:
                    exclude(row_number, row, "total row without an open bucket")
                    warnings.append(
                        f"Row {row_number}: bucket total {col_a!r} appeared "
                        "outside any bucket."
                    )
                    continue
                counts["bucket_total"] += 1
                declared = parse_amount(_cell(row, columns["open"]))
                if declared is None:
                    declared = parse_amount(_cell(row, columns["amount"]))
                close_bucket(declared)
                continue
            if current is not None:
                warnings.append(
                    f"Bucket {current.name!r} had no Total row; closed at "
                    f"row {row_number}."
                )
                close_bucket(None)
            counts["bucket"] += 1
            current = AgingBucket(name=col_a)
            buckets.append(current)
            continue

        # Data row: col 0 empty, must carry a parseable date.
        date_text = _cell(row, columns["date"])
        iso_date = _parse_date(date_text)
        if iso_date is None:
            raise AgingParseError(
                f"Row {row_number} couldn't be classified as an aging row, "
                f"bucket marker, total, or footer: {row!r}. Re-export the "
                "Aging Detail report from QBO."
            )
        if current is None:
            raise AgingParseError(
                f"Row {row_number}: aging row dated {date_text} appeared "
                "outside any bucket."
            )
        past_due_text = _cell(row, columns["past_due"])
        past_due_days: int | None = None
        if past_due_text:
            try:
                past_due_days = int(past_due_text.replace(",", ""))
            except ValueError:
                warnings.append(
                    f"Row {row_number}: unparseable Past due value "
                    f"{past_due_text!r}; stored as None."
                )
        counts["row"] += 1
        current.rows.append(
            AgingRow(
                txn_date=iso_date,
                txn_type=_cell(row, columns["type"]) or None,
                num=_cell(row, columns["num"]) or None,
                party=_cell(row, party_col) or None,
                due_date=_parse_date(_cell(row, columns["due"])),
                past_due_days=past_due_days,
                amount=parse_amount(_cell(row, columns["amount"])),
                open_balance=parse_amount(_cell(row, columns["open"])),
            )
        )

    if current is not None:
        warnings.append(
            f"Bucket {current.name!r} had no Total row; file ended mid-bucket."
        )
        close_bucket(None)

    declared_sum = sum(
        b.declared_total for b in buckets if b.declared_total is not None
    )
    if grand_total is not None and abs(declared_sum - grand_total) > BALANCE_TOLERANCE:
        warnings.append(
            f"Bucket totals sum to {declared_sum:.2f} but the report's grand "
            f"total is {grand_total:.2f}."
        )

    accounted = sum(counts.values())
    if accounted != len(rows):
        raise AgingParseError(
            f"Internal row reconciliation failed: {accounted} rows accounted "
            f"for out of {len(rows)} read. This is a parser bug — please "
            "report it."
        )

    return AgingParseResult(
        as_of_date=detection.as_of_date,
        side=side,
        buckets=buckets,
        grand_total=grand_total,
        warnings=warnings,
        excluded_rows=excluded_rows,
        row_count=len(rows),
        counts=counts,
    )
