"""Deterministic renderers: CONTEXT.md (facts) and ISSUES.md (flags).

Usage:
    uv run python -m knowledge.render --realm <realm_id> [--issues]

Writes data/rendered/<client-slug>/CONTEXT.md (and ISSUES.md with
--issues). data/ is gitignored — rendered client context never enters git.

DETERMINISM CONTRACT: the same database state must render to the same
BYTES, every time. Nothing wall-clock-dependent goes into CONTEXT.md
(the issues renderer takes an explicit as_of date for age math). All
orderings are total: facts sort by (taxonomy position, effective_date
NULLS LAST, created_at, id) — id is the final tiebreak, so equal
timestamps cannot reorder output. Superseded facts never render.

The header's active-fact count is the only line outside a category
section that changes when a fact is added (the gate's diff check
accounts for it).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
from dotenv import load_dotenv

from knowledge.store import CATEGORIES, Fact, get_active_facts
from rules import registry

SECTION_TITLES: dict[str, str] = {
    "entity_profile": "Entity profile",
    "operations": "Operations",
    "accounting_policy": "Accounting policy",
    "relationships": "Relationships",
    "preferences": "Preferences",
    "watch_items": "Watch items",
    "resolved_history": "Resolved history",
}
assert tuple(SECTION_TITLES) == CATEGORIES

SEVERITY_ORDER: tuple[str, ...] = ("critical", "warn", "info")
RESOLVED_WINDOW_DAYS: int = 30

# rule_code -> human title; sync-owned codes get fixed titles
_FLAG_TITLES: dict[str, str] = {
    rule.rule_code: rule.title for rule in registry.ALL_RULES
} | {
    "transform_warning": "Transform warning",
    "qbo_deleted": "Deleted in QBO",
}


def _client_name(conn: psycopg.Connection, client_id: UUID) -> str:
    row = conn.execute(
        "SELECT name FROM clients WHERE id = %s", (client_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"client {client_id} does not exist")
    return str(row[0])


def _fact_line(fact: Fact) -> str:
    if fact.effective_date is not None:
        tag = f"[{fact.source_type}, effective {fact.effective_date.isoformat()}]"
    else:
        tag = f"[{fact.source_type}]"
    return f"- {fact.statement} {tag}"


def render_context(conn: psycopg.Connection, client_id: UUID) -> str:
    """Markdown context document from active facts. Same DB state ->
    byte-identical output (see module docstring)."""
    name = _client_name(conn, client_id)
    facts = get_active_facts(conn, client_id)  # total order lives in the store
    plural = "fact" if len(facts) == 1 else "facts"
    lines: list[str] = [
        f"# Client Context — {name}",
        "",
        f"> Generated from {len(facts)} active {plural} on record."
        " Do not edit by hand — facts change only through the validated"
        " operations pipeline.",
    ]
    for category in CATEGORIES:
        lines += ["", f"## {SECTION_TITLES[category]}", ""]
        section = [fact for fact in facts if fact.category == category]
        if section:
            lines.extend(_fact_line(fact) for fact in section)
        else:
            lines.append("_No facts recorded._")
    return "\n".join(lines) + "\n"


def render_issues(
    conn: psycopg.Connection, client_id: UUID, as_of: date | None = None
) -> str:
    """Markdown issues document from flags: open by severity, then
    resolved-in-the-last-30-days. Deterministic for a fixed as_of."""
    as_of = as_of or date.today()
    name = _client_name(conn, client_id)

    open_rows = conn.execute(
        """
        SELECT severity, rule_code, source_type, source_ref,
               (%s - created_at::date) AS age_days, id
        FROM flags
        WHERE client_id = %s AND status = 'open'
        ORDER BY array_position(%s::text[], severity),
                 rule_code, created_at, source_ref, id
        """,
        (as_of, client_id, list(SEVERITY_ORDER)),
    ).fetchall()
    resolved_rows = conn.execute(
        """
        SELECT resolved_at::date, rule_code, source_type, source_ref,
               COALESCE(resolution_note, ''), id
        FROM flags
        WHERE client_id = %s AND status = 'resolved'
          AND resolved_at::date >= %s
        ORDER BY resolved_at DESC, rule_code, source_ref, id
        """,
        (client_id, as_of - timedelta(days=RESOLVED_WINDOW_DAYS)),
    ).fetchall()

    lines: list[str] = [
        f"# Issues — {name}",
        "",
        f"_As of {as_of.isoformat()}. Open flags by severity, ages in days._",
    ]
    for severity in SEVERITY_ORDER:
        group = [row for row in open_rows if row[0] == severity]
        lines += ["", f"## {severity.capitalize()} ({len(group)})", ""]
        if not group:
            lines.append("_None._")
        for _, rule_code, source_type, source_ref, age_days, _id in group:
            title = _FLAG_TITLES.get(rule_code, rule_code)
            lines.append(
                f"- [{rule_code}] {title} — {source_type} {source_ref}"
                f" (open {age_days}d)"
            )

    lines += ["", f"## Resolved (last {RESOLVED_WINDOW_DAYS} days)", ""]
    if not resolved_rows:
        lines.append("_None._")
    for resolved_on, rule_code, source_type, source_ref, note, _id in resolved_rows:
        title = _FLAG_TITLES.get(rule_code, rule_code)
        suffix = f": {note}" if note else ""
        lines.append(
            f"- {resolved_on.isoformat()} [{rule_code}] {title}"
            f" — {source_type} {source_ref}{suffix}"
        )
    return "\n".join(lines) + "\n"


def client_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "client"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Render CONTEXT/ISSUES markdown")
    parser.add_argument("--realm", required=True, help="QBO realm id")
    parser.add_argument("--issues", action="store_true",
                        help="also render ISSUES.md")
    args = parser.parse_args(argv)

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

        out_dir = (Path(__file__).resolve().parent.parent / "data" / "rendered"
                   / client_slug(name))
        out_dir.mkdir(parents=True, exist_ok=True)

        context_path = out_dir / "CONTEXT.md"
        context_path.write_text(render_context(conn, client_id))
        print(f"wrote {context_path}")
        if args.issues:
            issues_path = out_dir / "ISSUES.md"
            issues_path.write_text(render_issues(conn, client_id))
            print(f"wrote {issues_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
