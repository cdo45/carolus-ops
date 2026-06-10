"""Unit tests for the migration runner (no database required — the live
apply/no-op check is part of the phase gate, not CI)."""

from pathlib import Path

import pytest

from db.migrate import MIGRATIONS_DIR, discover_migrations


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
