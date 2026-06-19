"""Minimal SQL migration runner.

Plain .sql files in db/migrations/, named NNNN_description.sql, applied in
version order. Applied versions are tracked in schema_migrations. Each
migration runs in its own transaction together with its tracking row, so a
failed migration leaves nothing half-applied. No framework on purpose.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv

MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"

# Postgres extensions the migrations depend on. pg_trgm (migration 0008)
# backs trigram similarity for R027 duplicate-vendor detection and the
# fact near-duplicate gate. Preflight verifies these BEFORE applying any
# migration, so a missing extension fails with an actionable message
# instead of an obscure error partway through 0008.
REQUIRED_EXTENSIONS: tuple[str, ...] = ("pg_trgm",)


class PreflightError(RuntimeError):
    """A required Postgres extension is missing and cannot be enabled."""


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


def _extension_message(missing: list[str]) -> str:
    names = ", ".join(sorted(missing))
    first = sorted(missing)[0]
    return (
        f"required Postgres extension(s) not installed and not available on "
        f"this database: {names}. These ship with the postgresql-contrib "
        f"package (pg_trgm is a TRUSTED extension on Postgres 13+, so a "
        f"database owner can enable it without superuser). Install contrib on "
        f"the server, or enable it through your managed-Postgres provider's "
        f"extension catalog, then run as the database owner:  "
        f"CREATE EXTENSION {first};  and re-run `python -m db.migrate`."
    )


def check_extensions(
    conn: psycopg.Connection, required: Iterable[str] | None = None
) -> None:
    """Preflight: verify required extensions are installed or installable.

    pg_available_extensions lists every extension whose control file is
    present on the server, whether or not it is installed in this database —
    i.e. it is the catalog of what `CREATE EXTENSION` could enable here.
    Anything required but absent from it raises PreflightError with an
    actionable message. Read-only; safe to call repeatedly.
    """
    names = tuple(REQUIRED_EXTENSIONS if required is None else required)
    if not names:
        return
    rows = conn.execute(
        "SELECT name FROM pg_available_extensions WHERE name = ANY(%s)",
        (list(names),),
    ).fetchall()
    available = {row[0] for row in rows}
    missing = [name for name in names if name not in available]
    if missing:
        raise PreflightError(_extension_message(missing))


def migrate(database_url: str, up_to: str | None = None) -> list[Migration]:
    """Apply all pending migrations in order; return those applied.

    up_to: stop after applying this version (inclusive) — used by tests to
    stage a database at a historical schema state before applying the rest.
    """
    applied: list[Migration] = []
    with psycopg.connect(database_url) as conn:
        # preflight runs BEFORE anything is written (not even the tracking
        # table) so a missing extension aborts cleanly with no partial state
        check_extensions(conn)
        done = applied_versions(conn)
        conn.commit()
        for migration in discover_migrations():
            if up_to is not None and migration.version > up_to:
                break
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
    try:
        applied = migrate(database_url)
    except PreflightError as exc:
        print(f"migration preflight failed: {exc}", file=sys.stderr)
        return 3
    if applied:
        names = ", ".join(f"{m.version}_{m.name}" for m in applied)
        print(f"applied {len(applied)} migration(s): {names}")
    else:
        print("up to date — nothing to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
