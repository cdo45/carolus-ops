"""Shared helpers for QBO report parsers.

QBO exports share formatting quirks across report types: amounts carry
$/commas/quotes (parentheses for negatives in some reports), headers need
tolerant whitespace-insensitive matching, and every report ends with a junk
footer line that can masquerade as data. The footer takes two shapes:
"Accrual Basis <timestamp>" (GL and friends) or a bare leading-space
timestamp (" Friday, June 12, 2026 02:40 AM GMTZ") on AR/AP reports.
"""

from __future__ import annotations

import re

_FOOTER_PREFIXES = ("accrual basis", "cash basis")

_WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
)

JOB_PREFIX_RE = re.compile(r"^(\d+)-")


def parse_amount(text) -> float | None:
    """Parse a QBO amount: strips $/commas/quotes; (1,234.56) → -1234.56."""
    if text is None:
        return None
    s = str(text).strip().strip('"').replace("$", "").replace(",", "").strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()
    value = float(s)
    return -value if negative else value


def normalize_cell(cell: str) -> str:
    """Strip, lowercase, and collapse internal whitespace for comparisons."""
    return " ".join(cell.strip().lower().split())


def is_footer_row(row: list[str]) -> bool:
    """True for QBO's report footer, in either of its observed shapes:

    - "Accrual Basis Friday, June 12, ..." / "Cash Basis ..." (GL family)
    - a first cell that starts with whitespace and a weekday-name timestamp
      (" Friday, June 12, 2026 02:40 AM GMTZ"), all other cells empty
      (AR/AP aging and pairing reports)
    """
    if not row:
        return False
    first = row[0]
    norm = normalize_cell(first)
    if norm.startswith(_FOOTER_PREFIXES):
        return True
    return (
        bool(first)
        and first[:1].isspace()
        and norm.startswith(_WEEKDAYS)
        and all(not c.strip() for c in row[1:])
    )


def extract_job_prefix(num: str | None) -> str | None:
    """Leading digits + hyphen in a Num ("26013-0042" → "26013"), else None."""
    if not num:
        return None
    m = JOB_PREFIX_RE.match(num)
    return m.group(1) if m else None
