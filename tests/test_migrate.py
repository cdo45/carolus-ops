"""Migration runner tests.

Discovery/ordering tests are pure. The legacy-data test is DB-backed
(scratch fixture): it stages the database at migration 0001, inserts
rows the way Phase 1 actually wrote them, then applies everything else —
migrations must run clean over LIVE-SHAPED data, not just empty schemas.
(Regression: 0003 once UPDATEd severities before dropping the old check
constraint, which only a populated database could catch.)
"""

from pathlib import Path

import psycopg
import pytest
from psycopg.errors import CheckViolation

from db.migrate import (
    MIGRATIONS_DIR,
    REQUIRED_EXTENSIONS,
    PreflightError,
    check_extensions,
    discover_migrations,
    migrate,
)


def test_discovery_orders_by_version(tmp_path: Path) -> None:
    for name in ("0002_b.sql", "0001_a.sql", "0010_c.sql"):
        (tmp_path / name).write_text("select 1;")
    versions = [m.version for m in discover_migrations(tmp_path)]
    assert versions == ["0001", "0002", "0010"]


def test_discovery_rejects_bad_names(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("select 1;")
    with pytest.raises(ValueError, match="bad migration filename"):
        discover_migrations(tmp_path)


def test_discovery_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("select 1;")
    (tmp_path / "0001_b.sql").write_text("select 1;")
    with pytest.raises(ValueError, match="duplicate"):
        discover_migrations(tmp_path)


def test_repo_migrations_are_wellformed() -> None:
    migrations = discover_migrations(MIGRATIONS_DIR)
    assert migrations, "repo must contain at least migration 0001"
    assert migrations[0].version == "0001"


def test_preflight_passes_when_required_extensions_available(
    scratch_db_url: str,
) -> None:
    assert "pg_trgm" in REQUIRED_EXTENSIONS
    with psycopg.connect(scratch_db_url) as conn:
        check_extensions(conn)  # pg_trgm ships with the server: no raise
        check_extensions(conn, REQUIRED_EXTENSIONS)
        check_extensions(conn, ())  # empty requirement is a no-op


def test_preflight_fails_actionably_when_extension_absent(
    scratch_db_url: str,
) -> None:
    with psycopg.connect(scratch_db_url) as conn, pytest.raises(
        PreflightError
    ) as excinfo:
        check_extensions(conn, ("carolus_no_such_ext",))
    message = str(excinfo.value)
    assert "carolus_no_such_ext" in message, "names the missing extension"
    assert "CREATE EXTENSION" in message, "says how to enable it"
    assert "contrib" in message, "names where it comes from"


def test_migrate_aborts_before_applying_when_extension_missing(
    scratch_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight runs BEFORE any migration — not even schema_migrations is
    created — so a missing extension leaves zero partial state."""
    monkeypatch.setattr("db.migrate.REQUIRED_EXTENSIONS", ("carolus_no_such_ext",))

    with pytest.raises(PreflightError):
        migrate(scratch_db_url)

    with psycopg.connect(scratch_db_url) as conn:
        present = conn.execute(
            "SELECT to_regclass('public.schema_migrations')"
        ).fetchone()
    assert present == (None,), "preflight must abort before any migration runs"


def test_migrate_up_to_stops_at_version(scratch_db_url: str) -> None:
    applied = migrate(scratch_db_url, up_to="0002")
    assert [m.version for m in applied] == ["0001", "0002"]
    rest = migrate(scratch_db_url)
    assert rest and rest[0].version == "0003", "resumes after the staged point"


def test_migrations_apply_over_phase1_era_data(scratch_db_url: str) -> None:
    """Migrations must succeed against a database already containing data
    written under the 0001 schema — the state every live DB is in."""
    migrate(scratch_db_url, up_to="0001")

    with psycopg.connect(scratch_db_url) as conn:
        client = conn.execute(
            "INSERT INTO clients (name, qbo_realm_id) VALUES"
            " ('Legacy Co', 'legacy-realm') RETURNING id"
        ).fetchone()
        assert client is not None
        # flags exactly as Phase 1 sync wrote them: OLD severity vocabulary
        for severity in ("warning", "error", "info"):
            conn.execute(
                """
                INSERT INTO flags (client_id, rule_code, severity, status,
                                   source_type, source_ref, detail)
                VALUES (%s, 'transform_warning', %s, 'open', 'transaction',
                        %s, 'phase 1 era row')
                """,
                (client[0], severity, f"qbo:Invoice:{severity}"),
            )
        conn.commit()

    applied = migrate(scratch_db_url)  # 0002.. over populated tables
    assert [m.version for m in applied][:5] == [
        "0002", "0003", "0004", "0005", "0006",
    ]

    with psycopg.connect(scratch_db_url) as conn:
        rows = conn.execute(
            "SELECT source_ref, severity FROM flags ORDER BY source_ref"
        ).fetchall()
        assert rows == [
            ("qbo:Invoice:error", "critical"),
            ("qbo:Invoice:info", "info"),
            ("qbo:Invoice:warning", "warn"),
        ], "old vocabulary rewritten to the unified one"

        # and the new constraint actually enforces the new vocabulary
        with pytest.raises(CheckViolation):
            conn.execute(
                """
                INSERT INTO flags (client_id, rule_code, severity, status,
                                   source_type, source_ref, detail)
                VALUES (%s, 'x', 'warning', 'open', 'transaction', 'r', '')
                """,
                (client[0],),
            )
        conn.rollback()


def test_facts_taxonomy_migration_over_old_vocabulary(scratch_db_url: str) -> None:
    """0007 must remap facts written under the Phase 1 category enum —
    including surviving the append-only trigger, which blocks ordinary
    category rewrites."""
    migrate(scratch_db_url, up_to="0006")

    old_to_new = {
        "financial": "accounting_policy",
        "tax": "accounting_policy",
        "compliance": "accounting_policy",
        "operational": "operations",
        "preference": "preferences",
        "context": "entity_profile",
    }
    with psycopg.connect(scratch_db_url) as conn:
        client = conn.execute(
            "INSERT INTO clients (name) VALUES ('Taxonomy Co') RETURNING id"
        ).fetchone()
        assert client is not None
        for old_category in old_to_new:
            conn.execute(
                """
                INSERT INTO facts (client_id, category, statement,
                                   source_type, source_ref)
                VALUES (%s, %s, %s, 'carlos', 'carlos:setup-note')
                """,
                (client[0], old_category, f"legacy {old_category} fact"),
            )
        conn.commit()

    applied = migrate(scratch_db_url)
    assert applied and applied[0].version == "0007"

    with psycopg.connect(scratch_db_url) as conn:
        rows = conn.execute(
            "SELECT statement, category FROM facts ORDER BY statement"
        ).fetchall()
        assert {
            statement.removeprefix("legacy ").removesuffix(" fact"): category
            for statement, category in rows
        } == old_to_new, "every old category remapped to the agreed taxonomy"

        with pytest.raises(CheckViolation):  # old vocabulary now rejected
            conn.execute(
                """
                INSERT INTO facts (client_id, category, statement,
                                   source_type, source_ref)
                VALUES (%s, 'financial', 'x', 'carlos', 'carlos:n')
                """,
                (client[0],),
            )
        conn.rollback()

        # the trigger is re-enabled and still guards content (incl. the
        # new effective_date column)
        from psycopg.errors import RaiseException

        with pytest.raises(RaiseException, match="append-only"):
            conn.execute("UPDATE facts SET effective_date = '2026-01-01'")
        conn.rollback()
