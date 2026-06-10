"""Minimal SQL migration runner.

Plain .sql files in db/migrations/, named NNNN_description.sql, applied in
version order. Applied versions are tracked in schema_migrations. Each
migration runs in its own transaction together with its tracking row, so a
failed migration leaves nothing half-applied. No framework on purpose.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv

MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migrations sorted by version, validating names and uniqueness."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        version, _, name = path.stem.partition("_")
        if not (version.isascii() and version.isdigit()):
            raise ValueError(
                f"bad migration filename {path.name!r}: expected NNNN_description.sql"
            )
        migrations.append(Migration(version=version, name=name or path.stem, path=path))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration versions in {directory}")
    return migrations


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """Ensure the tracking table exists and return already-applied versions."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}

def migrate(database_url: str) -> list[Migration]:
    """Apply all pending migrations in order; return those applied."""
    applied: list[Migration] = []
    with psycopg.connect(database_url) as conn:
        done = applied_versions(conn)
        conn.commit()
        for migration in discover_migrations():
            if migration.version in done:
                continue
            with conn.transaction():
                conn.execute(migration.path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (migration.version,),
                )
            applied.append(migration)
    return applied


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    applied = migrate(database_url)
    if applied:
        names = ", ".join(f"{m.version}_{m.name}" for m in applied)
        print(f"applied {len(applied)} migration(s): {names}")
    else:
        print("up to date — nothing to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
