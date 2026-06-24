"""SQLite layer.

Per-client databases live at .appdata/clients/{slug}.db; the shared alias
database (factory + learned account-name mappings) lives at
.appdata/aliases.db; the client registry is .appdata/registry.json.

The .appdata location defaults to ./.appdata but can be overridden with the
QBO_APPDATA_DIR environment variable or a base_dir argument (used by tests).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    qbo_name TEXT NOT NULL,
    full_path TEXT,
    account_number TEXT,
    qbo_type TEXT,
    detail_type TEXT,
    category TEXT,
    confidence INTEGER,
    status TEXT CHECK(status IN ('proposed','confirmed','unsure')) DEFAULT 'proposed',
    proposed_number TEXT,
    proposed_name TEXT,
    dormant INTEGER DEFAULT 0,
    inactive_candidate INTEGER DEFAULT 0,
    merge_into INTEGER REFERENCES accounts(id),
    coa_balance REAL,
    created_at TEXT,
    UNIQUE(qbo_name)
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    report_type TEXT,
    period_start TEXT,
    period_end TEXT,
    as_of_date TEXT,
    filename TEXT,
    row_count INTEGER,
    validation_result TEXT,
    superseded_data BLOB,
    uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    txn_date TEXT NOT NULL,
    txn_type TEXT,
    num TEXT,
    name TEXT,
    description TEXT,
    split TEXT,
    amount REAL NOT NULL,
    running_balance REAL,
    job_prefix TEXT,
    upload_id INTEGER REFERENCES uploads(id)
);
CREATE INDEX IF NOT EXISTS idx_transactions_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_job_prefix ON transactions(job_prefix);

CREATE TABLE IF NOT EXISTS ar_aging_snapshots (
    id INTEGER PRIMARY KEY,
    as_of_date TEXT UNIQUE,
    upload_id INTEGER REFERENCES uploads(id)
);

CREATE TABLE IF NOT EXISTS ar_aging_rows (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER REFERENCES ar_aging_snapshots(id),
    customer TEXT,
    invoice_date TEXT,
    due_date TEXT,
    num TEXT,
    amount REAL,
    open_balance REAL,
    bucket TEXT
);
CREATE INDEX IF NOT EXISTS idx_ar_aging_rows_snapshot_id ON ar_aging_rows(snapshot_id);

CREATE TABLE IF NOT EXISTS ap_aging_snapshots (
    id INTEGER PRIMARY KEY,
    as_of_date TEXT UNIQUE,
    upload_id INTEGER REFERENCES uploads(id)
);

CREATE TABLE IF NOT EXISTS ap_aging_rows (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER REFERENCES ap_aging_snapshots(id),
    vendor TEXT,
    invoice_date TEXT,
    due_date TEXT,
    num TEXT,
    amount REAL,
    open_balance REAL,
    bucket TEXT
);
CREATE INDEX IF NOT EXISTS idx_ap_aging_rows_snapshot_id ON ap_aging_rows(snapshot_id);

CREATE TABLE IF NOT EXISTS invoice_payments (
    id INTEGER PRIMARY KEY,
    customer TEXT,
    row_type TEXT CHECK(row_type IN ('invoice','payment','credit','other')),
    date TEXT,
    num TEXT,
    amount REAL,
    group_key TEXT,
    upload_id INTEGER REFERENCES uploads(id)
);
CREATE INDEX IF NOT EXISTS idx_invoice_payments_group_key ON invoice_payments(group_key);

CREATE TABLE IF NOT EXISTS bills_payments (
    id INTEGER PRIMARY KEY,
    vendor TEXT,
    row_type TEXT CHECK(row_type IN ('invoice','payment','credit','other')),
    date TEXT,
    num TEXT,
    amount REAL,
    group_key TEXT,
    upload_id INTEGER REFERENCES uploads(id)
);
CREATE INDEX IF NOT EXISTS idx_bills_payments_group_key ON bills_payments(group_key);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS forecast_runs (
    id INTEGER PRIMARY KEY,
    run_date TEXT,
    scenario TEXT
);

CREATE TABLE IF NOT EXISTS forecast_rows (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES forecast_runs(id),
    week INTEGER,
    row_type TEXT,
    amount REAL,
    confidence_tier TEXT
);
CREATE INDEX IF NOT EXISTS idx_forecast_rows_run_id ON forecast_rows(run_id);

CREATE TABLE IF NOT EXISTS variance_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    week_ending TEXT,
    row_type TEXT,
    forecast REAL,
    actual REAL,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    entity TEXT,
    entity_id INTEGER,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    source TEXT CHECK(source IN ('classifier','user','import','system'))
);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity, entity_id);

CREATE TABLE IF NOT EXISTS kpi_history (
    id INTEGER PRIMARY KEY,
    as_of TEXT,
    key TEXT,
    value REAL,
    UNIQUE(as_of, key)
);
"""

GLOBAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY,
    pattern TEXT,
    category TEXT,
    confidence INTEGER,
    source TEXT CHECK(source IN ('factory','learned')),
    origin_client TEXT,
    note TEXT,
    flag_if_nonzero INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_aliases_pattern ON aliases(pattern);
"""


def resource_path(*parts: str) -> Path:
    """Locate a bundled read-only resource (data/, static/) whether running
    from source or from a PyInstaller bundle (where files live in _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root.joinpath(*parts)


DATA_DIR = resource_path("data")


def appdata_root(base_dir: str | Path | None = None) -> Path:
    """Resolve the data directory.

    Order: explicit param > QBO_APPDATA_DIR env > a visible per-user folder
    when running as a packaged app > ./.appdata when running from source.
    """
    if base_dir is not None:
        return Path(base_dir)
    env = os.environ.get("QBO_APPDATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path.home() / "QBO KPI Dashboard"
    return Path(".appdata")


def client_dir(slug: str, base_dir: str | Path | None = None) -> Path:
    """The single folder that holds everything for one client: its database,
    uploaded exports, generated dashboards, and COA proposals."""
    return appdata_root(base_dir) / "clients" / slug


def _migrate_layout(root: Path, slug: str) -> None:
    """Move a client from the old split layout (clients/{slug}.db plus
    work/{slug}/) into one consolidated folder (clients/{slug}/). Idempotent
    and safe: only moves a file when the destination doesn't already exist."""
    cdir = root / "clients" / slug
    old_db = root / "clients" / f"{slug}.db"
    new_db = cdir / "client.db"
    if old_db.exists() and not new_db.exists():
        cdir.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            src = root / "clients" / f"{slug}.db{suffix}"
            if src.exists():
                src.rename(cdir / f"client.db{suffix}")
    old_work = root / "work" / slug
    if old_work.exists() and old_work.is_dir():
        cdir.mkdir(parents=True, exist_ok=True)
        for item in old_work.iterdir():
            dest = cdir / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
        try:
            old_work.rmdir()
        except OSError:
            pass


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_client(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "account_number" not in existing:
        conn.execute("ALTER TABLE accounts ADD COLUMN account_number TEXT")


def get_client_db(slug: str, base_dir: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the per-client database for `slug`, stored in
    that client's own folder."""
    _migrate_layout(appdata_root(base_dir), slug)
    conn = _connect(client_dir(slug, base_dir) / "client.db")
    conn.executescript(CLIENT_SCHEMA)
    _migrate_client(conn)
    conn.commit()
    return conn


def get_global_db(base_dir: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared alias database, seeding factory aliases on first create."""
    conn = _connect(appdata_root(base_dir) / "aliases.db")
    conn.executescript(GLOBAL_SCHEMA)
    seeded = conn.execute(
        "SELECT 1 FROM aliases WHERE source = 'factory' LIMIT 1"
    ).fetchone()
    if seeded is None:
        _seed_factory_aliases(conn)
    conn.commit()
    return conn


def _seed_factory_aliases(conn: sqlite3.Connection) -> None:
    with open(DATA_DIR / "factory_aliases.json", encoding="utf-8") as f:
        aliases = json.load(f)
    conn.executemany(
        """
        INSERT INTO aliases (pattern, category, confidence, source, flag_if_nonzero, created_at)
        VALUES (:pattern, :category, :confidence, :source, :flag_if_nonzero, datetime('now'))
        """,
        [
            {
                "pattern": a["pattern"],
                "category": a["category"],
                "confidence": a["confidence"],
                "source": a["source"],
                "flag_if_nonzero": int(a.get("flag_if_nonzero", False)),
            }
            for a in aliases
        ],
    )


def load_registry(base_dir: str | Path | None = None) -> dict:
    """Read .appdata/registry.json; returns an empty registry if missing."""
    path = appdata_root(base_dir) / "registry.json"
    if not path.exists():
        return {"clients": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry: dict, base_dir: str | Path | None = None) -> None:
    """Write the registry atomically (temp file + rename)."""
    path = appdata_root(base_dir) / "registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)
    tmp.replace(path)
