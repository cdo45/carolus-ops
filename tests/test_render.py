"""Renderer determinism (byte-identical), total ordering under ties,
superseded exclusion, fixed section skeleton, and issues grouping."""

from __future__ import annotations

from datetime import date, datetime, timezone
from difflib import unified_diff
from uuid import UUID

import psycopg

from knowledge.render import client_slug, render_context, render_issues
from knowledge.store import add_fact, supersede_fact
from tests.conftest import make_client

AS_OF = date(2026, 6, 10)


def seed_typical(conn: psycopg.Connection, client_id: UUID) -> None:
    add_fact(conn, client_id, category="entity_profile",
             statement="S-corp electrical contractor with 12 employees",
             source_type="carlos", source_ref="carlos:onboarding",
             effective_date=date(2026, 1, 1))
    add_fact(conn, client_id, category="operations",
             statement="Foreman texts receipt photos every Friday",
             source_type="email", source_ref="msg-1")
    add_fact(conn, client_id, category="preferences",
             statement="Wants the monthly summary on the first Tuesday",
             source_type="carlos", source_ref="carlos:call-3")
    conn.commit()


def test_repeated_renders_are_byte_identical(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    seed_typical(conn, client_id)
    first = render_context(conn, client_id)
    assert first == render_context(conn, client_id)
    assert first.encode() == render_context(conn, client_id).encode()


def test_tied_sort_keys_cannot_reorder_output(conn: psycopg.Connection) -> None:
    """Three facts with IDENTICAL (category, effective_date, created_at):
    the id tiebreak makes the order total — provably unambiguous."""
    client_id = make_client(conn)
    stamp = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    statements = ["Tie case alpha statement", "Tie case bravo statement",
                  "Tie case charlie statement"]
    ids: dict[str, str] = {}
    for statement in statements:
        row = conn.execute(
            """
            INSERT INTO facts (client_id, category, statement, source_type,
                               source_ref, effective_date, created_at)
            VALUES (%s, 'watch_items', %s, 'carlos', 'carlos:tie',
                    '2026-05-01', %s)
            RETURNING id
            """,
            (client_id, statement, stamp),
        ).fetchone()
        assert row is not None
        ids[statement] = str(row[0])
    conn.commit()

    rendered = render_context(conn, client_id)
    rendered_order = [s for s in statements if s in rendered]
    positions = {s: rendered.index(s) for s in statements}
    by_position = sorted(rendered_order, key=lambda s: positions[s])
    expected = sorted(statements, key=lambda s: ids[s])
    assert by_position == expected, "ties must resolve by id, totally"
    for _ in range(5):
        assert render_context(conn, client_id) == rendered


def test_superseded_facts_never_render(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    old = add_fact(conn, client_id, category="operations",
                   statement="Runs two crews on residential work",
                   source_type="carlos", source_ref="carlos:n1")
    supersede_fact(conn, old.id,
                   statement="Runs four crews after the Henderson hire",
                   source_type="carlos", source_ref="carlos:n2")
    conn.commit()

    rendered = render_context(conn, client_id)
    assert "Runs two crews on residential work" not in rendered
    assert "Runs four crews after the Henderson hire" in rendered


def test_fixed_section_skeleton(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)  # zero facts
    rendered = render_context(conn, client_id)
    headings = [line for line in rendered.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Entity profile", "## Operations", "## Accounting policy",
        "## Relationships", "## Preferences", "## Watch items",
        "## Resolved history",
    ], "every category section present exactly once, fixed order"
    assert rendered.count("_No facts recorded._") == 7
    assert "Generated from 0 active facts" in rendered


def test_new_fact_changes_only_its_section_and_count(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    seed_typical(conn, client_id)
    before = render_context(conn, client_id)
    add_fact(conn, client_id, category="watch_items",
             statement="County retainage release still pending since April",
             source_type="email", source_ref="msg-9")
    conn.commit()
    after = render_context(conn, client_id)

    changed = [line[1:] for line in unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0,
    ) if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    for line in changed:
        assert ("active fact" in line  # header count line
                or "County retainage" in line
                or line == "_No facts recorded._"), (
            f"unexpected line changed: {line!r}"
        )


def flag(
    conn: psycopg.Connection, client_id: UUID, *, rule_code: str = "R010",
    severity: str = "critical", status: str = "open",
    source_ref: str = "ref-1", created: str = "2026-06-01",
    resolved: str | None = None, note: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status,
                           source_type, source_ref, detail, created_at,
                           resolved_at, resolution_note)
        VALUES (%s, %s, %s, %s, 'transaction', %s, '{}', %s, %s, %s)
        """,
        (client_id, rule_code, severity, status, source_ref,
         f"{created}T12:00:00+00:00",
         f"{resolved}T12:00:00+00:00" if resolved else None, note),
    )


def test_issues_grouping_ages_and_resolved_window(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    flag(conn, client_id, rule_code="R010", severity="critical",
         source_ref="txn-a", created="2026-05-29")  # 12 days old
    flag(conn, client_id, rule_code="R017", severity="info",
         source_ref="txn-b", created="2026-06-08")
    flag(conn, client_id, rule_code="transform_warning", severity="warn",
         source_ref="qbo:Invoice:7", created="2026-06-01")
    flag(conn, client_id, rule_code="R015", severity="warn", status="resolved",
         source_ref="txn-c", created="2026-05-01", resolved="2026-06-08",
         note="condition cleared on 2026-06-08")
    flag(conn, client_id, rule_code="R013", severity="warn", status="resolved",
         source_ref="acct-d", created="2026-03-01", resolved="2026-04-20",
         note="fixed long ago")  # outside the 30-day window
    conn.commit()

    rendered = render_issues(conn, client_id, as_of=AS_OF)
    assert rendered == render_issues(conn, client_id, as_of=AS_OF)

    assert "## Critical (1)" in rendered
    assert "## Warn (1)" in rendered
    assert "## Info (1)" in rendered
    assert "[R010] Possible duplicate payment — transaction txn-a (open 12d)" \
        in rendered
    assert "[transform_warning] Transform warning" in rendered, "fallback title"
    critical_at = rendered.index("## Critical")
    warn_at = rendered.index("## Warn")
    info_at = rendered.index("## Info")
    assert critical_at < warn_at < info_at, "severity order fixed"

    assert "2026-06-08 [R015] Stale uncategorized transaction" in rendered
    assert "condition cleared on 2026-06-08" in rendered
    assert "fixed long ago" not in rendered, "outside 30-day window"


def test_client_slug() -> None:
    assert client_slug("Acme Builders, LLC") == "acme-builders-llc"
    assert client_slug("---") == "client"
