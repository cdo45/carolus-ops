"""QBO Chart of Accounts / Account List parser.

Real "Account List" exports have only TWO title rows (no period line), so
the header row is found by content — the row containing both "Type" and
"Detail type" — never by fixed index. The account-number, description, and
balance columns may be absent entirely; the balance column is titled
"Total balance" or "Balance". A name column and a Type column are the only
hard requirements. Account names carry the full colon hierarchy
("FIXED ASSETS:Vehicles:2019 Range Rover"), unlike the GL, which shows
leaf names only. A report-level TOTAL row near the end is excluded, not
an account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.detect import read_rows
from core.parsers.common import is_footer_row, normalize_cell, parse_amount

BAD_COA_MESSAGE = (
    "This file doesn't look like a QBO Chart of Accounts export — in QBO go "
    "to Reports → Account List, export to CSV."
)

# Header labels QBO uses, normalized → logical column.
_NUMBER_LABELS = {"account #", "number", "account number"}
_NAME_LABELS = {"full name", "account", "account name", "name"}
_TYPE_LABELS = {"type", "account type"}
_DETAIL_LABELS = {"detail type"}
_DESCRIPTION_LABELS = {"description"}
_BALANCE_LABELS = {"balance", "quickbooks balance", "total balance"}

_HEADER_SCAN_LIMIT = 8


class COAParseError(Exception):
    """Raised when a COA file is structurally unusable."""


@dataclass
class COAAccount:
    full_path: str
    leaf_name: str
    qbo_name: str  # = full_path; the unique identity for matching
    account_number: str | None
    qbo_type: str | None
    detail_type: str | None
    balance: float | None
    description: str | None = None
    deleted_marker: bool = False


@dataclass
class COAParseResult:
    accounts: list[COAAccount]
    warnings: list[str]
    excluded_rows: list[dict]
    row_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)


def _map_columns(header: list[str]) -> dict[str, int | None]:
    """Locate logical columns in the header row; tolerant of absences."""
    columns: dict[str, int | None] = {
        "number": None,
        "name": None,
        "type": None,
        "detail": None,
        "description": None,
        "balance": None,
    }
    for i, cell in enumerate(header):
        label = normalize_cell(cell)
        if label in _NUMBER_LABELS and columns["number"] is None:
            columns["number"] = i
        elif label in _NAME_LABELS and columns["name"] is None:
            columns["name"] = i
        elif label in _TYPE_LABELS and columns["type"] is None:
            columns["type"] = i
        elif label in _DETAIL_LABELS and columns["detail"] is None:
            columns["detail"] = i
        elif label in _DESCRIPTION_LABELS and columns["description"] is None:
            columns["description"] = i
        elif label in _BALANCE_LABELS and columns["balance"] is None:
            columns["balance"] = i
    return columns


def _find_header(rows: list[list[str]]) -> int | None:
    """Find the header row by content within the first few rows: it's the
    one containing both "Type" and "Detail type" cells. Real Account List
    exports have no period line, so the index isn't fixed."""
    for i in range(min(_HEADER_SCAN_LIMIT, len(rows))):
        cells = {normalize_cell(c) for c in rows[i]}
        if "type" in cells and "detail type" in cells:
            return i
    return None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def parse_coa(filepath: str | Path) -> COAParseResult:
    rows = read_rows(filepath)
    header_idx = _find_header(rows)
    if header_idx is None:
        raise COAParseError(BAD_COA_MESSAGE)

    columns = _map_columns(rows[header_idx])
    if columns["name"] is None or columns["type"] is None:
        raise COAParseError(BAD_COA_MESSAGE)

    warnings: list[str] = []
    excluded_rows: list[dict] = []
    accounts: list[COAAccount] = []
    counts = {
        "preamble": header_idx,
        "header": 1,
        "account": 0,
        "excluded": 0,
    }
    if columns["number"] is None:
        warnings.append("No account-number column in this export.")
    if columns["balance"] is None:
        warnings.append("No balance column in this export.")

    def exclude(row_number: int, row: list[str], reason: str) -> None:
        counts["excluded"] += 1
        excluded_rows.append(
            {"row_number": row_number, "raw": row, "reason": reason}
        )

    for idx in range(header_idx + 1, len(rows)):
        row = rows[idx]
        row_number = idx + 1

        if not any(c.strip() for c in row):
            exclude(row_number, row, "blank row")
            continue
        if is_footer_row(row):
            exclude(row_number, row, "report footer")
            continue

        full_path = _cell(row, columns["name"])
        qbo_type = _cell(row, columns["type"])

        # Report-level TOTAL row: "TOTAL" in the name cell, or (when an
        # Account # column leads) in the first cell with an empty Type.
        first_cell = row[0].strip() if row else ""
        if normalize_cell(full_path) == "total" or (
            not qbo_type and normalize_cell(first_cell) == "total"
        ):
            exclude(row_number, row, "report total row")
            continue

        if not full_path or not qbo_type:
            # Unaccounted row: hard error, same silent-row-drop defense as GL.
            raise COAParseError(
                f"Row {row_number} couldn't be classified as an account, "
                f"blank row, or footer: {row!r}. Re-export the Account List "
                "from QBO."
            )

        counts["account"] += 1
        accounts.append(
            COAAccount(
                full_path=full_path,
                leaf_name=full_path.split(":")[-1].strip(),
                qbo_name=full_path,
                account_number=_cell(row, columns["number"]) or None,
                qbo_type=qbo_type,
                detail_type=_cell(row, columns["detail"]) or None,
                balance=parse_amount(_cell(row, columns["balance"])),
                description=_cell(row, columns["description"]) or None,
                deleted_marker="(deleted)" in full_path.lower(),
            )
        )

    accounted = sum(counts.values())
    if accounted != len(rows):
        raise COAParseError(
            f"Internal row reconciliation failed: {accounted} rows accounted "
            f"for out of {len(rows)} read. This is a parser bug — please "
            "report it."
        )

    return COAParseResult(
        accounts=accounts,
        warnings=warnings,
        excluded_rows=excluded_rows,
        row_count=len(rows),
        counts=counts,
    )
