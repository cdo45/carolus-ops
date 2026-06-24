"""Account classification engine.

Assigns each account a taxonomy category, a confidence (0-100), and a
source tag via a strict waterfall — the FIRST stage that produces a
category wins:

  stage 1  alias library (global db: factory + learned), matched on the
           LEAF name; the most specific (longest) matching pattern wins
  stage 2  keyword rules (data/keyword_rules.json) in priority order over
           "name + detail type", honoring each rule's type-class condition;
           the file's empty-pattern catch-all is a stage-5 default, not a
           keyword rule, and is skipped here
  stage 3  detail-type table (data/detail_type_map.json), exact match
  stage 4  qbo_type table (Bank → CASH 95, Income → REV 95, ...)
  stage 5  defaults (COGS → DMAT 60, Expenses → OH 75, Other Expense → OH 70)
  stage 6  unmapped: category NULL, confidence 0 → review queue

The classifier never touches accounts whose status is 'confirmed' and never
changes status itself — status is the user's alone. It writes category and
confidence back and audit-logs every change.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from core.db import resource_path

DATA_DIR = resource_path("data")

# Stage 4: qbo_type → (category, confidence).
QBO_TYPE_MAP: dict[str, tuple[str, int]] = {
    "bank": ("CASH", 95),
    "accounts receivable (a/r)": ("AR", 95),
    "other current assets": ("OCA", 90),
    "fixed assets": ("FA", 95),
    "accounts payable (a/p)": ("AP", 95),
    "credit card": ("CC", 95),
    "other current liabilities": ("OCL", 80),
    "long term liabilities": ("LTD", 95),
    "equity": ("EQ", 90),
    "income": ("REV", 95),
    "other income": ("OI", 95),
}

# Stage 5: qbo_type → (category, confidence) defaults.
DEFAULT_MAP: dict[str, tuple[str, int]] = {
    "cost of goods sold": ("DMAT", 60),
    "expenses": ("OH", 75),
    "expense": ("OH", 75),
    "other expense": ("OH", 70),
}

# Type classes referenced by keyword-rule conditions.
BALANCE_SHEET_TYPES = {
    "bank",
    "accounts receivable (a/r)",
    "other current assets",
    "fixed assets",
    "other assets",
    "accounts payable (a/p)",
    "credit card",
    "other current liabilities",
    "long term liabilities",
    "equity",
}
EXPENSE_TYPES = {"expenses", "expense", "cost of goods sold", "other expense"}


@dataclass
class Classification:
    category: str | None
    confidence: int
    source: str
    tier: str


@dataclass
class ClassifyReport:
    total: int = 0
    skipped_confirmed: int = 0
    by_tier: dict[str, int] = field(default_factory=lambda: {
        "auto": 0, "confirm": 0, "queue": 0})
    by_source: dict[str, int] = field(default_factory=dict)
    queue: list[tuple[str, str | None, str | None]] = field(
        default_factory=list)


def tier_for(category: str | None, confidence: int | None) -> str:
    """auto (>=90) / confirm (70-89) / queue (<70 or no category)."""
    if category is None or confidence is None:
        return "queue"
    if confidence >= 90:
        return "auto"
    if confidence >= 70:
        return "confirm"
    return "queue"


@lru_cache(maxsize=1)
def _keyword_rules() -> tuple:
    with open(DATA_DIR / "keyword_rules.json", encoding="utf-8") as f:
        rules = json.load(f)
    # Empty-pattern catch-alls (the COGS default) belong to stage 5.
    return tuple(
        r for r in rules if any(p.strip() for p in r.get("patterns", []))
    )


@lru_cache(maxsize=1)
def _detail_type_map() -> dict:
    with open(DATA_DIR / "detail_type_map.json", encoding="utf-8") as f:
        return json.load(f)


def _condition_met(condition: dict | None, qbo_type_lower: str) -> bool:
    if not condition:
        return True
    type_class = condition.get("type_class")
    if type_class == "balance_sheet" and qbo_type_lower not in BALANCE_SHEET_TYPES:
        return False
    if type_class == "expense" and qbo_type_lower not in EXPENSE_TYPES:
        return False
    if "qbo_type" in condition and qbo_type_lower != condition["qbo_type"].lower():
        return False
    if "qbo_type_not" in condition and qbo_type_lower == condition["qbo_type_not"].lower():
        return False
    return True


def _match_alias(leaf: str, aliases: list) -> tuple | None:
    """Alias matches when the leaf EQUALS the pattern, or starts with the
    pattern followed by " (" — covering parenthetical suffixes like
    "Sales (deleted)" or "Money Market (8556)". A bare-space continuation
    ("Sales Tax Payable", "Sales of Product Income") is a different account
    name and must NOT match. The most specific (longest) pattern wins."""
    best = None
    for pattern, category, confidence, source in aliases:
        p = pattern.strip().lower()
        if not p:
            continue
        if leaf == p or leaf.startswith(p + " ("):
            if best is None or len(p) > len(best[0]):
                best = (p, category, confidence, source)
    return best


def classify_account(
    name: str,
    qbo_type: str | None,
    detail_type: str | None,
    aliases: list,
) -> Classification:
    """Pure waterfall, stages 1-6 (stage 0, skipping confirmed accounts,
    is the caller's job). aliases: (pattern, category, confidence, source)
    tuples, source 'factory' or 'learned'."""
    leaf = name.split(":")[-1].strip().lower()
    qbo_type_lower = (qbo_type or "").strip().lower()

    # Stage 1 — alias library.
    hit = _match_alias(leaf, aliases)
    if hit:
        _, category, confidence, source = hit
        c = Classification(
            category=category,
            confidence=int(confidence),
            source=f"alias-{source}",
            tier="",
        )
        c.tier = tier_for(c.category, c.confidence)
        return c

    # Stage 2 — keyword rules, first match wins.
    text = f"{name.strip().lower()} {(detail_type or '').strip().lower()}"
    for rule in _keyword_rules():
        if not _condition_met(rule.get("condition"), qbo_type_lower):
            continue
        if any(p and p in text for p in rule["patterns"]):
            return Classification(
                category=rule["category"],
                confidence=int(rule["confidence"]),
                source="keyword",
                tier=tier_for(rule["category"], rule["confidence"]),
            )

    # Stage 3 — detail-type table.
    detail_lower = (detail_type or "").strip().lower()
    if detail_lower in _detail_type_map():
        category = _detail_type_map()[detail_lower]
        return Classification(category, 85, "detail-type",
                              tier_for(category, 85))

    # Stage 4 — qbo_type table.
    if qbo_type_lower in QBO_TYPE_MAP:
        category, confidence = QBO_TYPE_MAP[qbo_type_lower]
        return Classification(category, confidence, "qbo-type",
                              tier_for(category, confidence))

    # Stage 5 — defaults.
    if qbo_type_lower in DEFAULT_MAP:
        category, confidence = DEFAULT_MAP[qbo_type_lower]
        return Classification(category, confidence, "default",
                              tier_for(category, confidence))

    # Stage 6 — unmapped: review queue.
    return Classification(None, 0, "unmapped", "queue")


def load_aliases(global_conn: sqlite3.Connection) -> list:
    return [
        (r["pattern"], r["category"], r["confidence"], r["source"])
        for r in global_conn.execute(
            "SELECT pattern, category, confidence, source FROM aliases"
        )
    ]


def classify_client(
    conn: sqlite3.Connection, global_conn: sqlite3.Connection
) -> ClassifyReport:
    """Classify every non-confirmed account; write category/confidence back
    and audit each change. One transaction; re-running with nothing changed
    produces zero new audit rows."""
    import datetime as dt

    aliases = load_aliases(global_conn)
    report = ClassifyReport()
    report.skipped_confirmed = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE status = 'confirmed'"
    ).fetchone()[0]

    rows = conn.execute(
        "SELECT id, qbo_name, qbo_type, detail_type, category, confidence "
        "FROM accounts WHERE status != 'confirmed'"
    ).fetchall()
    report.total = report.skipped_confirmed + len(rows)

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        for row in rows:
            c = classify_account(
                row["qbo_name"], row["qbo_type"], row["detail_type"], aliases
            )
            report.by_tier[c.tier] += 1
            report.by_source[c.source] = report.by_source.get(c.source, 0) + 1
            if c.tier == "queue":
                report.queue.append(
                    (row["qbo_name"], row["qbo_type"], row["detail_type"])
                )
            if c.category != row["category"] or c.confidence != row["confidence"]:
                conn.execute(
                    "UPDATE accounts SET category = ?, confidence = ? "
                    "WHERE id = ?",
                    (c.category, c.confidence, row["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO audit_log
                        (ts, entity, entity_id, field, old_value, new_value,
                         source)
                    VALUES (?, 'accounts', ?, 'category', ?, ?, 'classifier')
                    """,
                    (
                        now,
                        row["id"],
                        json.dumps({"category": row["category"],
                                    "confidence": row["confidence"]},
                                   separators=(",", ":")),
                        json.dumps({"category": c.category,
                                    "confidence": c.confidence},
                                   separators=(",", ":")),
                    ),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def main(argv: list[str] | None = None) -> None:
    """Manual-testing entry: python -m core.classify <client_slug>"""
    import argparse

    from core import db

    parser = argparse.ArgumentParser(
        description="Classify a client's accounts and print the report."
    )
    parser.add_argument("client_slug", help="client database slug")
    args = parser.parse_args(argv)

    conn = db.get_client_db(args.client_slug)
    global_conn = db.get_global_db()
    try:
        report = classify_client(conn, global_conn)
    finally:
        conn.close()
        global_conn.close()

    print(f"Accounts: {report.total} "
          f"({report.skipped_confirmed} confirmed, skipped)")
    print("Tiers:   "
          + "  ".join(f"{t}={n}" for t, n in report.by_tier.items()))
    print("Sources: "
          + "  ".join(f"{s}={n}" for s, n in sorted(report.by_source.items())))
    if report.queue:
        print(f"Queue ({len(report.queue)} accounts, first 15):")
        for qbo_name, qbo_type, detail_type in report.queue[:15]:
            print(f"  {qbo_name}  [type={qbo_type or '-'}, "
                  f"detail={detail_type or '-'}]")
    else:
        print("Queue: empty")


if __name__ == "__main__":
    main()
