"""QBO General Ledger parser.

Parses a QBO General Ledger export (CSV or XLSX) into per-account sections
with verified running balances. Every input row must be accounted for as a
transaction, section marker, beginning balance, total, header/preamble, or an
explicitly excluded row — anything unrecognized is a hard error so rows can
never be dropped silently.
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

__all__ = [
    "GLParseError",
    "GLTransaction",
    "GLAccount",
    "GLParseResult",
    "parse_gl",
    "parse_amount",
    "BAD_HEADER_MESSAGE",
    "BALANCE_TOLERANCE",
]

BALANCE_TOLERANCE = 0.005

EXPECTED_HEADER = [
    "",
    "transaction date",
    "transaction type",
    "num",
    "name",
    "description",
    "split",
    "amount",
    "balance",
]

BAD_HEADER_MESSAGE = (
    "This file doesn't look like a QBO General Ledger export — re-export "
    "using QBO's standard General Ledger report."
)

class GLParseError(Exception):
    """Raised when a GL file is structurally unusable."""


@dataclass
class GLTransaction:
    txn_date: str
    txn_type: str | None
    num: str | None
    name: str | None
    description: str | None
    split: str | None
    amount: float
    running_balance: float | None
    job_prefix: str | None


@dataclass
class GLAccount:
    name: str
    beginning_balance: float | None = None
    # How beginning_balance was determined: "labeled" (a "Beginning Balance"
    # row), "unlabeled" (a balance-only first row), or "derived" (computed
    # from the first transaction). None for zero-transaction sections.
    beginning_balance_source: str | None = None
    ending_balance: float | None = None
    # The "Total for X" row's Amount column: the account's net activity for
    # the period (sum of transaction amounts), NOT the ending balance.
    declared_net_activity: float | None = None
    transactions: list[GLTransaction] = field(default_factory=list)


@dataclass
class GLParseResult:
    accounts: list[GLAccount]
    period_start: str | None
    period_end: str | None
    row_count: int
    warnings: list[str]
    excluded_rows: list[dict]
    # QBO renders zero-dollar transactions with an empty Amount cell.
    zero_amount_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)


def _parse_date(text: str) -> str | None:
    """MM/DD/YYYY → ISO date string, or None if not a date."""
    try:
        return dt.datetime.strptime(text.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _validate_header(row: list[str], warnings: list[str]) -> None:
    cells = [normalize_cell(c) for c in row]
    cells += [""] * (len(EXPECTED_HEADER) - len(cells))
    mismatches = [
        i
        for i, expected in enumerate(EXPECTED_HEADER)
        if cells[i] != expected
    ]
    mismatches += list(range(len(EXPECTED_HEADER), len(cells)))
    if len(mismatches) >= 3:
        raise GLParseError(BAD_HEADER_MESSAGE)
    if mismatches:
        cols = ", ".join(str(i + 1) for i in mismatches)
        warnings.append(
            f"Header row differs from the standard GL layout in column(s) "
            f"{cols}; proceeding anyway."
        )


def parse_gl(filepath: str | Path) -> GLParseResult:
    rows = read_rows(filepath)
    detection = detect_from_rows(rows)
    header_idx = detection.header_row_index
    if header_idx is None:
        raise GLParseError(BAD_HEADER_MESSAGE)

    warnings: list[str] = []
    excluded_rows: list[dict] = []
    accounts: list[GLAccount] = []
    zero_amount_count = 0
    counts = {
        "preamble": header_idx,
        "header": 1,
        "section": 0,
        "beginning_balance": 0,
        "transaction": 0,
        "total": 0,
        "excluded": 0,
    }

    _validate_header(rows[header_idx], warnings)

    current: GLAccount | None = None
    explicit_beginning = False  # current section had a Beginning Balance row

    def exclude(row_number: int, row: list[str], reason: str) -> None:
        counts["excluded"] += 1
        excluded_rows.append(
            {"row_number": row_number, "raw": row, "reason": reason}
        )

    def close_section(declared_net_activity: float | None) -> None:
        nonlocal current
        if current is None:
            return
        acct = current
        acct.declared_net_activity = declared_net_activity
        if acct.transactions:
            first = acct.transactions[0]
            if acct.beginning_balance is None:
                # No Beginning Balance row: derive it from the first
                # transaction's running balance.
                if first.running_balance is not None:
                    acct.beginning_balance = round(
                        first.running_balance - first.amount, 2
                    )
                    acct.beginning_balance_source = "derived"
            acct.ending_balance = acct.transactions[-1].running_balance
        else:
            if acct.beginning_balance is None:
                acct.beginning_balance = 0.0
            acct.ending_balance = acct.beginning_balance
            warnings.append(
                f"Account {acct.name!r} has no transactions in this period."
            )
        if acct.declared_net_activity is not None:
            beginning = acct.beginning_balance or 0.0
            if acct.ending_balance is not None and (
                abs(beginning + acct.declared_net_activity - acct.ending_balance)
                > BALANCE_TOLERANCE
            ):
                warnings.append(
                    f"Account {acct.name!r}: beginning balance {beginning:.2f} "
                    f"plus net activity {acct.declared_net_activity:.2f} "
                    f"doesn't match the ending balance "
                    f"{acct.ending_balance:.2f}."
                )
            # The stronger per-section completeness check: catches dropped
            # rows even when the balances look right.
            txn_sum = sum(t.amount for t in acct.transactions)
            if abs(acct.declared_net_activity - txn_sum) > BALANCE_TOLERANCE:
                warnings.append(
                    f"Account {acct.name!r}: the report's net activity "
                    f"{acct.declared_net_activity:.2f} doesn't match the sum "
                    f"of parsed transactions {txn_sum:.2f} — rows may be "
                    "missing."
                )
        current = None

    for idx in range(header_idx + 1, len(rows)):
        row = rows[idx]
        row_number = idx + 1  # 1-based, matches what a user sees in a spreadsheet
        cells = row + [""] * (len(EXPECTED_HEADER) - len(row))
        col_a = cells[0].strip()

        if not any(c.strip() for c in cells):
            exclude(row_number, row, "blank row")
            continue

        if is_footer_row(cells):
            exclude(row_number, row, "report footer")
            continue

        if col_a:
            if normalize_cell(col_a).startswith("total for"):
                if current is None:
                    exclude(row_number, row, "total row without an open section")
                    warnings.append(
                        f"Row {row_number}: total row {col_a!r} appeared "
                        "outside any account section."
                    )
                    continue
                counts["total"] += 1
                # Net activity lives in the Amount column on Total rows;
                # fall back to Balance for nonstandard layouts.
                declared = parse_amount(cells[7])
                if declared is None:
                    declared = parse_amount(cells[8])
                close_section(declared)
                continue
            # New account section.
            if current is not None:
                warnings.append(
                    f"Account {current.name!r} had no Total row; section "
                    f"closed at row {row_number}."
                )
                close_section(None)
            counts["section"] += 1
            current = GLAccount(name=col_a)
            accounts.append(current)
            explicit_beginning = False
            continue

        # col A empty: beginning balance or transaction.
        col_b = cells[1].strip()
        if normalize_cell(col_b) == "beginning balance":
            if current is None:
                raise GLParseError(
                    f"Row {row_number}: Beginning Balance row appeared outside "
                    "any account section."
                )
            counts["beginning_balance"] += 1
            current.beginning_balance = parse_amount(cells[8])
            current.beginning_balance_source = "labeled"
            explicit_beginning = True
            continue

        # Unlabeled beginning balance: real QBO GL exports can open a section
        # with a row that is empty everywhere except the Balance column. Only
        # the FIRST content row of a section may be read that way; the same
        # shape anywhere else is skipped (logged), never guessed at.
        if (
            not col_b
            and not any(c.strip() for c in cells[2:8])
            and cells[8].strip()
        ):
            if (
                current is not None
                and not current.transactions
                and current.beginning_balance is None
            ):
                counts["beginning_balance"] += 1
                current.beginning_balance = parse_amount(cells[8])
                current.beginning_balance_source = "unlabeled"
                explicit_beginning = True
            else:
                where = f" in account {current.name!r}" if current else ""
                exclude(row_number, row, "balance-only row mid-section")
                warnings.append(
                    f"Row {row_number}: balance-only row appeared mid-section"
                    f"{where}; skipped rather than guessed."
                )
            continue

        iso_date = _parse_date(col_b)
        if iso_date is not None:
            if current is None:
                raise GLParseError(
                    f"Row {row_number}: transaction dated {col_b} appeared "
                    "outside any account section."
                )
            amount = parse_amount(cells[7])
            if amount is None:
                # QBO renders zero-dollar transactions (e.g. a $0 Payment
                # closing a written-off invoice) with an empty Amount cell.
                amount = 0.0
                zero_amount_count += 1
            balance = parse_amount(cells[8])
            if balance is None:
                # Never observed; the integrity gate depends on Balance, so
                # don't guess.
                raise GLParseError(
                    f"Row {row_number}: transaction dated {col_b} in account "
                    f"{current.name!r} has no Balance value — re-export using "
                    "QBO's standard General Ledger report."
                )
            num = cells[3].strip() or None
            txn = GLTransaction(
                txn_date=iso_date,
                txn_type=cells[2].strip() or None,
                num=num,
                name=cells[4].strip() or None,
                description=cells[5].strip() or None,
                split=cells[6].strip() or None,
                amount=amount,
                running_balance=balance,
                job_prefix=extract_job_prefix(num),
            )

            # Running-balance integrity gate.
            prior: float | None = None
            if current.transactions:
                prior = current.transactions[-1].running_balance
            elif explicit_beginning:
                prior = current.beginning_balance
            if (
                prior is not None
                and balance is not None
                and abs(prior + amount - balance) > BALANCE_TOLERANCE
            ):
                raise GLParseError(
                    f"The GL file looks incomplete around {col_b} in account "
                    f"{current.name} — re-export the full date range without "
                    "filters."
                )

            counts["transaction"] += 1
            current.transactions.append(txn)
            continue

        # Empty col A, col B is neither a date nor "Beginning Balance":
        # an unaccounted row is a hard error (silent-row-drop defense).
        raise GLParseError(
            f"Row {row_number} couldn't be classified as a transaction, "
            f"section marker, beginning balance, total, or footer: {row!r}. "
            "Re-export using QBO's standard General Ledger report."
        )

    if current is not None:
        warnings.append(
            f"Account {current.name!r} had no Total row; file ended "
            "mid-section."
        )
        close_section(None)

    # Row-count reconciliation: every row classified exactly once.
    accounted = sum(counts.values())
    if accounted != len(rows):
        raise GLParseError(
            f"Internal row reconciliation failed: {accounted} rows accounted "
            f"for out of {len(rows)} read. This is a parser bug — please "
            "report it."
        )

    if zero_amount_count:
        warnings.append(
            f"{zero_amount_count} zero-amount transactions "
            "(rendered with empty Amount by QBO)"
        )

    return GLParseResult(
        accounts=accounts,
        period_start=detection.period_start,
        period_end=detection.period_end,
        row_count=len(rows),
        warnings=warnings,
        excluded_rows=excluded_rows,
        zero_amount_count=zero_amount_count,
        counts=counts,
    )
