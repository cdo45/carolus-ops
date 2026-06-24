"""CHUNK 11 tests: the COA standardization proposal."""

import csv

import pytest

from core import db
from core.classify import classify_client
from core.coa_proposal import generate_proposal, write_proposal_files
from core.importer import dormancy_pass, import_coa, import_gl
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from tests.test_importer import write_coa, write_gl


def _build(tmp_path, slug, coa, gl=None):
    conn = db.get_client_db(slug, base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    import_coa(conn, parse_coa(write_coa(tmp_path / (slug + ".csv"), coa)))
    if gl:
        import_gl(conn, parse_gl(write_gl(tmp_path / (slug + "_gl.csv"), gl,
                  period="January, 2026-January, 2026")), "gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    global_conn.close()
    return conn


def _by_name(result):
    return {r.qbo_name: r for r in result.rows}


def test_keeps_existing_in_range_number(tmp_path):
    conn = _build(tmp_path, "keep", [
        ("1001", "Checking", "Bank", "Checking", 1000.00),
    ])
    rows = _by_name(generate_proposal(conn))
    conn.close()
    assert rows["Checking"].proposed_number == 1001
    assert rows["Checking"].action == "keep"


def test_assigns_gapped_numbers(tmp_path):
    conn = _build(tmp_path, "gap", [
        ("", "Checking", "Bank", "Checking", 1000.00),
        ("", "Savings", "Bank", "Savings", 500.00),
    ])
    rows = _by_name(generate_proposal(conn))
    conn.close()
    nums = sorted([rows["Checking"].proposed_number,
                   rows["Savings"].proposed_number])
    assert nums == [1000, 1010]  # next-free in steps of 10


def test_parent_child_numbers_coherent(tmp_path):
    conn = _build(tmp_path, "tree", [
        ("", "Job Materials", "Cost of Goods Sold",
         "Supplies & materials - COGS", 200.00),
        ("", "Job Materials:Lumber", "Cost of Goods Sold",
         "Supplies & materials - COGS", 60.00),
    ])
    rows = _by_name(generate_proposal(conn))
    conn.close()
    parent = rows["Job Materials"].proposed_number
    child = rows["Job Materials:Lumber"].proposed_number
    assert 5300 <= parent <= 5399
    assert child == parent + 1  # child sits in the parent's decade


def test_merge_fires_on_near_duplicates_only(tmp_path):
    conn = _build(tmp_path, "merge", [
        ("", "Truck Fuel", "Expenses", "Auto", 120.00),
        ("", "Truck Fuel Expense", "Expenses", "Auto", 80.00),
        ("", "Internet", "Expenses", "Utilities", 50.00),
    ])
    rows = _by_name(generate_proposal(conn))
    conn.close()
    assert rows["Truck Fuel Expense"].action == "merge"
    assert rows["Truck Fuel Expense"].merge_into == "Truck Fuel"
    # The kept duplicate and the distinct account are never merged.
    assert rows["Truck Fuel"].action != "merge"
    assert rows["Internet"].action != "merge"


def test_deactivate_list(tmp_path):
    conn = _build(tmp_path, "deact", [
        ("", "Old Account (deleted)", "Expenses", "Office", 0.00),
        ("", "Zero Dormant", "Expenses", "Office", 0.00),
        ("", "Active Expense", "Expenses", "Office", 100.00),
    ])
    result = generate_proposal(conn)
    conn.close()
    deactivated = {r.qbo_name for r in result.rows if r.action == "deactivate"}
    assert "Zero Dormant" in deactivated
    assert any("Old Account" in n for n in deactivated)  # inactive (deleted)
    assert "Active Expense" not in deactivated


def test_qbo_import_csv_shape(tmp_path):
    conn = _build(tmp_path, "csvtest", [
        ("", "Checking", "Bank", "Checking", 1000.00),
        ("", "Design Income", "Income", "Service/Fee Income", 0.00),
        ("", "Zero Dormant", "Expenses", "Office", 0.00),  # deactivate
    ])
    result = generate_proposal(conn)
    files = write_proposal_files(conn, tmp_path / "out")
    conn.close()

    with open(files["csv"], newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["Account Number", "Account Name", "Type", "Detail Type"]
    numbered = [r for r in result.rows if r.proposed_number is not None]
    assert len(rows) - 1 == len(numbered)  # only numbered rows, no deactivates
    for data in rows[1:]:
        assert len(data) == 4
        assert data[0].isdigit()
    html = files["html"].read_text(encoding="utf-8")
    assert html.count("<html") == 1
    assert "http://" not in html and "https://" not in html
