"""Tests for the SQLite layer (core/db.py)."""

import json
import sqlite3

import pytest

from core import db

EXPECTED_CLIENT_TABLES = {
    "accounts",
    "uploads",
    "transactions",
    "ar_aging_snapshots",
    "ar_aging_rows",
    "ap_aging_snapshots",
    "ap_aging_rows",
    "invoice_payments",
    "bills_payments",
    "config",
    "forecast_runs",
    "forecast_rows",
    "variance_log",
    "audit_log",
}


def test_client_schema_creates_clean(tmp_path):
    conn = db.get_client_db("testco", base_dir=tmp_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert EXPECTED_CLIENT_TABLES <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (tmp_path / "clients" / "testco" / "client.db").exists()
    finally:
        conn.close()


def test_client_schema_idempotent(tmp_path):
    db.get_client_db("testco", base_dir=tmp_path).close()
    conn = db.get_client_db("testco", base_dir=tmp_path)
    conn.close()


def test_registry_round_trip(tmp_path):
    assert db.load_registry(base_dir=tmp_path) == {"clients": []}
    registry = {"clients": [{"slug": "acme", "name": "Acme Construction"}]}
    db.save_registry(registry, base_dir=tmp_path)
    assert db.load_registry(base_dir=tmp_path) == registry


def test_factory_aliases_seed_matches_json(tmp_path):
    with open(db.DATA_DIR / "factory_aliases.json", encoding="utf-8") as f:
        expected = json.load(f)

    conn = db.get_global_db(base_dir=tmp_path)
    try:
        rows = conn.execute(
            "SELECT pattern, category, confidence, source, flag_if_nonzero "
            "FROM aliases ORDER BY id"
        ).fetchall()
        assert len(rows) == len(expected)
        for row, exp in zip(rows, expected):
            assert row["pattern"] == exp["pattern"]
            assert row["category"] == exp["category"]
            assert row["confidence"] == exp["confidence"]
            assert row["source"] == "factory"
            assert row["flag_if_nonzero"] == int(exp["flag_if_nonzero"])
    finally:
        conn.close()


def test_factory_aliases_seed_only_once(tmp_path):
    db.get_global_db(base_dir=tmp_path).close()
    conn = db.get_global_db(base_dir=tmp_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
        with open(db.DATA_DIR / "factory_aliases.json", encoding="utf-8") as f:
            assert count == len(json.load(f))
    finally:
        conn.close()


def test_audit_log_accepts_row(tmp_path):
    conn = db.get_client_db("testco", base_dir=tmp_path)
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, entity, entity_id, field, old_value, "
            "new_value, source) VALUES (datetime('now'), 'accounts', 1, "
            "'category', 'OH', 'DMAT', 'user')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["entity"] == "accounts"
        assert row["new_value"] == "DMAT"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_log (ts, entity, source) "
                "VALUES (datetime('now'), 'accounts', 'not-a-valid-source')"
            )
    finally:
        conn.close()


def test_foreign_keys_enforced(tmp_path):
    conn = db.get_client_db("testco", base_dir=tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO transactions (account_id, txn_date, amount) "
                "VALUES (9999, '2026-01-01', 100.0)"
            )
    finally:
        conn.close()


def test_legacy_layout_migrates_into_one_folder(tmp_path):
    import sqlite3
    # Old split layout: clients/acme.db + work/acme/exports/file.csv
    (tmp_path / "clients").mkdir()
    sqlite3.connect(tmp_path / "clients" / "acme.db").close()
    (tmp_path / "work" / "acme" / "exports").mkdir(parents=True)
    (tmp_path / "work" / "acme" / "exports" / "gl.csv").write_text("x")

    db.get_client_db("acme", base_dir=tmp_path).close()

    folder = db.client_dir("acme", base_dir=tmp_path)
    assert (folder / "client.db").exists()
    assert (folder / "exports" / "gl.csv").read_text() == "x"
    assert not (tmp_path / "clients" / "acme.db").exists()
    assert not (tmp_path / "work" / "acme").exists()
