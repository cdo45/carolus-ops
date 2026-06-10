"""Engine flag lifecycle + plumbing rules (R000/R001).

Lifecycle tests drive the engine with fake in-memory rules; R000/R001 run
against factory-seeded canonical rows. DB-backed via the scratch fixture.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest

from rules import r000_unbalanced_lines, r001_orphan_lines, registry
from rules.base import Finding, InvalidRule, UnsourcedFinding, validate_rule
from rules.engine import AUTO_RESOLVE_PREFIX, run_rules
from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account, make_txn

AS_OF = date(2026, 6, 10)


def fake_rule(
    code: str = "T900", severity: str = "warn"
) -> tuple[Any, dict[str, list[Finding]]]:
    holder: dict[str, list[Finding]] = {"findings": []}
    rule = SimpleNamespace(
        rule_code=code,
        severity=severity,
        title="Fake rule",
        description="lifecycle test rule",
        run=lambda conn, client_id, as_of: list(holder["findings"]),
    )
    return rule, holder


def flag_rows(
    conn: psycopg.Connection, client_id: UUID, rule_code: str
) -> list[tuple[str, str, str | None]]:
    return conn.execute(
        """
        SELECT source_ref, status, resolution_note FROM flags
        WHERE client_id = %s AND rule_code = %s ORDER BY created_at
        """,
        (client_id, rule_code),
    ).fetchall()


# ------------------------------------------------------------ pure contract


def test_validate_rule_rejects_missing_attrs() -> None:
    with pytest.raises(InvalidRule, match="missing attributes"):
        validate_rule(SimpleNamespace(rule_code="X"))


def test_validate_rule_rejects_bad_severity() -> None:
    rule, _ = fake_rule(severity="warning")  # old vocabulary must be rejected
    with pytest.raises(InvalidRule, match="severity"):
        validate_rule(rule)


def test_registry_is_validated() -> None:
    assert [rule.rule_code for rule in registry.ALL_RULES][:2] == ["R000", "R001"]


# ------------------------------------------------------------ lifecycle (DB)


def test_new_finding_creates_open_flag(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    rule, holder = fake_rule()
    holder["findings"] = [
        Finding("transaction", "ref-1", {"amount": "12.00", "why": "test"})
    ]

    summary = run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    assert summary["rules"]["T900"] == {
        "findings": 1, "new": 1, "unchanged": 0, "resolved": 0, "suppressed": 0,
    }
    row = conn.execute(
        """
        SELECT severity, status, source_type, source_ref, detail FROM flags
        WHERE client_id = %s AND rule_code = 'T900'
        """,
        (client_id,),
    ).fetchone()
    assert row == (
        "warn", "open", "transaction", "ref-1",
        json.dumps({"amount": "12.00", "why": "test"}, sort_keys=True),
    )


def test_rerun_is_idempotent(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    rule, holder = fake_rule()
    holder["findings"] = [Finding("transaction", "ref-1")]

    run_rules(conn, client_id, as_of=AS_OF, rules=[rule])
    again = run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    assert again["rules"]["T900"]["new"] == 0
    assert again["rules"]["T900"]["unchanged"] == 1
    assert len(flag_rows(conn, client_id, "T900")) == 1, "no duplicate open flags"


def test_auto_resolve_then_refire_creates_fresh_flag(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    rule, holder = fake_rule()
    holder["findings"] = [Finding("transaction", "ref-1")]
    run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    holder["findings"] = []  # condition cleared
    cleared = run_rules(conn, client_id, as_of=AS_OF, rules=[rule])
    assert cleared["rules"]["T900"]["resolved"] == 1
    rows = flag_rows(conn, client_id, "T900")
    assert rows == [("ref-1", "resolved", f"{AUTO_RESOLVE_PREFIX}{AS_OF.isoformat()}")]
    resolved_at = conn.execute(
        "SELECT resolved_at FROM flags WHERE client_id = %s AND rule_code = 'T900'",
        (client_id,),
    ).fetchone()
    assert resolved_at is not None and resolved_at[0] is not None

    holder["findings"] = [Finding("transaction", "ref-1")]  # new occurrence
    refire = run_rules(conn, client_id, as_of=AS_OF, rules=[rule])
    assert refire["rules"]["T900"]["new"] == 1
    statuses = [status for _, status, _ in flag_rows(conn, client_id, "T900")]
    assert statuses == ["resolved", "open"], "auto-resolved keys may re-fire"


def test_dismissed_key_is_never_reopened(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    rule, holder = fake_rule()
    holder["findings"] = [Finding("transaction", "ref-1")]
    run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    conn.execute(
        """
        UPDATE flags SET status = 'dismissed', resolution_note = 'not an issue'
        WHERE client_id = %s AND rule_code = 'T900'
        """,
        (client_id,),
    )
    conn.commit()

    again = run_rules(conn, client_id, as_of=AS_OF, rules=[rule])
    assert again["rules"]["T900"]["suppressed"] == 1
    assert again["rules"]["T900"]["new"] == 0
    assert [s for _, s, _ in flag_rows(conn, client_id, "T900")] == ["dismissed"]


def test_manually_resolved_key_is_never_reopened(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    rule, holder = fake_rule()
    holder["findings"] = [Finding("transaction", "ref-1")]
    run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    conn.execute(
        """
        UPDATE flags SET status = 'resolved', resolved_at = now(),
                         resolution_note = 'fixed by Carlos in QBO'
        WHERE client_id = %s AND rule_code = 'T900'
        """,
        (client_id,),
    )
    conn.commit()

    again = run_rules(conn, client_id, as_of=AS_OF, rules=[rule])
    assert again["rules"]["T900"]["suppressed"] == 1
    assert [s for _, s, _ in flag_rows(conn, client_id, "T900")] == ["resolved"]


def test_unsourced_finding_fails_the_run(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    rule, holder = fake_rule()
    holder["findings"] = [Finding("transaction", "   ")]

    with pytest.raises(UnsourcedFinding):
        run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    failed = conn.execute(
        "SELECT count(*) FROM runs WHERE client_id = %s AND routine ="
        " 'rules_engine' AND status = 'failed'",
        (client_id,),
    ).fetchone()
    assert failed is not None and failed[0] == 1
    assert flag_rows(conn, client_id, "T900") == [], "failed run writes no flags"


def test_engine_does_not_touch_foreign_rule_codes(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status, source_type,
                           source_ref, detail)
        VALUES (%s, 'transform_warning', 'warn', 'open', 'transaction',
                'qbo:Invoice:1', 'sync-owned')
        """,
        (client_id,),
    )
    conn.commit()
    rule, _ = fake_rule()  # registered code T900 with zero findings

    run_rules(conn, client_id, as_of=AS_OF, rules=[rule])

    status = conn.execute(
        "SELECT status FROM flags WHERE client_id = %s AND rule_code ="
        " 'transform_warning'",
        (client_id,),
    ).fetchone()
    assert status == ("open",), "sync-owned flags keep their own lifecycle"


# ------------------------------------------------------------ R000 / R001


def test_r000_fires_on_unbalanced_only(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Bank", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")
    unbalanced = make_txn(
        conn, client_id, amount="100.00",
        lines=[{"account": expense, "amount": "100.00", "posting": "debit"}],
    )
    balanced_purchase(conn, client_id, bank=bank, expense=expense)
    lineless = make_txn(conn, client_id, amount="42.00", lines=[])
    conn.commit()

    findings = r000_unbalanced_lines.run(conn, client_id, AS_OF)

    refs = {finding.source_ref for finding in findings}
    assert refs == {str(unbalanced), str(lineless)}


def test_r001_fires_on_dead_accounts_only(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Bank", acct_type="Bank")
    inactive = make_account(conn, client_id, name="Old Expense", active=False)
    deleted = make_account(
        conn, client_id, name="Deleted Expense",
        qbo_deleted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    live = make_account(conn, client_id, name="Live Expense")

    fires_inactive = balanced_purchase(conn, client_id, bank=bank, expense=inactive)
    fires_deleted = balanced_purchase(conn, client_id, bank=bank, expense=deleted)
    balanced_purchase(conn, client_id, bank=bank, expense=live)
    conn.commit()

    findings = r001_orphan_lines.run(conn, client_id, AS_OF)

    refs = {finding.source_ref for finding in findings}
    assert refs == {str(fires_inactive), str(fires_deleted)}
    detail = next(
        f.detail for f in findings if f.source_ref == str(fires_inactive)
    )
    assert detail["dead_accounts"] == ["Old Expense"]


def test_engine_end_to_end_with_registry_rules(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    expense = make_account(conn, client_id, name="Office")
    txn = make_txn(
        conn, client_id, amount="50.00",
        lines=[{"account": expense, "amount": "50.00", "posting": "debit"}],
    )
    conn.commit()

    summary = run_rules(conn, client_id, as_of=AS_OF)

    assert summary["rules"]["R000"]["new"] == 1
    open_flag = conn.execute(
        "SELECT source_ref FROM flags WHERE client_id = %s AND rule_code = 'R000'"
        " AND status = 'open'",
        (client_id,),
    ).fetchone()
    assert open_flag == (str(txn),)
