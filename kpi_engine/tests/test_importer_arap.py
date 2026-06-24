"""Tests for the AR/AP importers (import_aging, import_pairings)."""

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from core import db
from core.importer import import_aging, import_pairings
from core.parsers.aging import parse_aging
from core.parsers.pairings import parse_pairings

FIXTURES = Path(__file__).parent / "fixtures"
AR_AGING = FIXTURES / "ar_aging_detail.csv"
AP_AGING = FIXTURES / "ap_aging_detail.csv"
INVOICES = FIXTURES / "invoices_payments.csv"
BILLS = FIXTURES / "bills_payments.csv"


@pytest.fixture
def conn(tmp_path):
    connection = db.get_client_db("testco", base_dir=tmp_path / "appdata")
    yield connection
    connection.close()


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_aging_snapshot_replace_same_date(conn):
    first = import_aging(conn, parse_aging(AR_AGING), "ar.csv")
    assert first.replaced is False
    assert first.inserted_count == 8
    rows_before = count(conn, "ar_aging_rows")

    second = import_aging(conn, parse_aging(AR_AGING), "ar.csv")
    assert second.replaced is True
    assert second.snapshot_id == first.snapshot_id
    assert second.deleted_count == rows_before
    assert count(conn, "ar_aging_snapshots") == 1
    assert count(conn, "ar_aging_rows") == rows_before
    assert count(conn, "uploads") == 2
    # The snapshot now points at the newest upload.
    snap = conn.execute(
        "SELECT upload_id FROM ar_aging_snapshots WHERE id = ?",
        (first.snapshot_id,),
    ).fetchone()
    assert snap["upload_id"] == second.upload_id


def test_aging_snapshot_accumulates_new_date(conn, tmp_path):
    import_aging(conn, parse_aging(AR_AGING), "ar1.csv")
    later = tmp_path / "ar_later.csv"
    later.write_text(
        AR_AGING.read_text(encoding="utf-8").replace(
            "As of Jun 11, 2026", "As of Jun 18, 2026"
        ),
        encoding="utf-8",
    )
    report = import_aging(conn, parse_aging(later), "ar2.csv")
    assert report.replaced is False
    assert count(conn, "ar_aging_snapshots") == 2
    assert count(conn, "ar_aging_rows") == 16  # both snapshots retained
    dates = {
        r["as_of_date"]
        for r in conn.execute("SELECT as_of_date FROM ar_aging_snapshots")
    }
    assert dates == {"2026-06-11", "2026-06-18"}


def test_ap_aging_lands_in_vendor_tables(conn):
    report = import_aging(conn, parse_aging(AP_AGING), "ap.csv")
    assert report.inserted_count == 4
    assert count(conn, "ap_aging_rows") == 4
    assert count(conn, "ar_aging_rows") == 0
    row = conn.execute(
        "SELECT vendor, bucket, open_balance FROM ap_aging_rows "
        "WHERE num = '26013-0042'"
    ).fetchone()
    assert row["vendor"] == "BuildSupply Co"
    assert row["bucket"] == "1 - 30 days past due"
    assert row["open_balance"] == pytest.approx(800.00)
    upload = conn.execute(
        "SELECT report_type, as_of_date FROM uploads"
    ).fetchone()
    assert upload["report_type"] == "AP_AGING"
    assert upload["as_of_date"] == "2026-06-11"


def test_pairings_identical_reimport_zero_diff(conn):
    first = import_pairings(conn, parse_pairings(INVOICES), "ip.csv")
    assert first.inserted_count == 8
    second = import_pairings(conn, parse_pairings(INVOICES), "ip.csv")
    assert (second.diff.changed_count, second.diff.added_count,
            second.diff.removed_count) == (0, 0, 0)
    assert count(conn, "invoice_payments") == 8
    assert count(conn, "uploads") == 2


def test_pairings_edited_diff_and_supersede(conn, tmp_path):
    import_pairings(conn, parse_pairings(INVOICES), "ip1.csv")
    edited = tmp_path / "ip_edited.csv"
    edited.write_text(
        INVOICES.read_text(encoding="utf-8").replace(
            '"7,510.00",Unpaid,61.85', '"7,510.00",Unpaid,0.00'
        ).replace(
            ',07/15/2025,Invoice,Kitchen remodel,1060,"7,510.00"',
            ',07/15/2025,Invoice,Kitchen remodel,1060,"7,500.00"',
        ),
        encoding="utf-8",
    )
    report = import_pairings(conn, parse_pairings(edited), "ip2.csv")
    assert report.diff.changed_count == 1
    sample = report.diff.changed_samples[0]
    assert sample["party"] == "Customer B"
    assert sample["old_amount"] == pytest.approx(7510.00)
    assert sample["new_amount"] == pytest.approx(7500.00)

    blob = conn.execute(
        "SELECT superseded_data FROM uploads WHERE id = 1"
    ).fetchone()[0]
    old_rows = json.loads(gzip.decompress(blob))
    assert len(old_rows) == 8
    assert any(r["amount"] == pytest.approx(7510.00) for r in old_rows)


def test_bills_row_types_and_group_keys_persisted(conn):
    import_pairings(conn, parse_pairings(BILLS), "bp.csv")
    rows = conn.execute(
        "SELECT vendor, row_type, group_key FROM bills_payments "
        "WHERE vendor = 'Card Services' ORDER BY id"
    ).fetchall()
    assert [r["row_type"] for r in rows] == [
        "payment", "credit", "other", "other", "invoice",
    ]
    assert {r["group_key"] for r in rows} == {"Card Services::1"}
    hardware = conn.execute(
        "SELECT COUNT(DISTINCT group_key) AS n FROM bills_payments "
        "WHERE vendor = 'Hardware Hut'"
    ).fetchone()
    assert hardware["n"] == 1


def test_pairings_atomicity_rollback(conn):
    import_pairings(conn, parse_pairings(INVOICES), "ip.csv")
    before_rows = count(conn, "invoice_payments")
    before_uploads = count(conn, "uploads")

    poisoned = parse_pairings(INVOICES)
    # CHECK constraint violation at insert time — after the range DELETE.
    poisoned.parties[0].rows[0].row_type = "bogus"
    with pytest.raises(sqlite3.IntegrityError):
        import_pairings(conn, poisoned, "ip_poisoned.csv")

    assert count(conn, "invoice_payments") == before_rows
    assert count(conn, "uploads") == before_uploads
    amounts = [
        r["amount"]
        for r in conn.execute("SELECT amount FROM invoice_payments")
    ]
    assert any(a == pytest.approx(4368.00) for a in amounts)


def test_aging_atomicity_rollback(conn):
    import_aging(conn, parse_aging(AR_AGING), "ar.csv")
    before_rows = count(conn, "ar_aging_rows")
    before_uploads = count(conn, "uploads")

    poisoned = parse_aging(AR_AGING)
    poisoned.buckets[0].rows[0].txn_date = {"not": "a string"}  # bind error
    with pytest.raises(sqlite3.Error):
        import_aging(conn, poisoned, "ar_poisoned.csv")

    assert count(conn, "ar_aging_rows") == before_rows
    assert count(conn, "uploads") == before_uploads
    assert count(conn, "ar_aging_snapshots") == 1


def test_aging_requires_as_of_date(conn):
    result = parse_aging(AR_AGING)
    result.as_of_date = None
    with pytest.raises(ValueError):
        import_aging(conn, result, "ar.csv")
