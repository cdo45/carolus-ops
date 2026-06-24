"""Tests for the Chart of Accounts parser (core/parsers/coa.py)."""

from pathlib import Path

import pytest

from core.detect import detect_report_type
from core.parsers.coa import BAD_COA_MESSAGE, COAParseError, parse_coa

FIXTURES = Path(__file__).parent / "fixtures"
COA_FULL = FIXTURES / "coa_full.csv"
COA_MINIMAL = FIXTURES / "coa_minimal.csv"


def test_both_shapes_detect_as_coa():
    assert detect_report_type(COA_FULL).report_type == "COA"
    assert detect_report_type(COA_MINIMAL).report_type == "COA"


def test_header_found_by_content_not_index(tmp_path):
    # Real Account List: header at index 3 (no period line) — both fixtures.
    assert parse_coa(COA_FULL).counts["preamble"] == 3
    # The header can sit anywhere in the first 8 rows.
    shifted = tmp_path / "coa_shifted.csv"
    shifted.write_text(
        "Acme Services Inc,,,\n"
        "Account List,,,\n"
        ",,,\n"
        ",,,\n"
        ",,,\n"
        "Full name,Type,Detail type,Total balance\n"
        "Checking,Bank,Checking,500.00\n",
        encoding="utf-8",
    )
    result = parse_coa(shifted)
    assert result.counts["preamble"] == 5
    assert result.accounts[0].full_path == "Checking"
    assert result.accounts[0].balance == pytest.approx(500.00)


def test_no_header_in_first_rows_raises(tmp_path):
    bad = tmp_path / "headerless.csv"
    bad.write_text("a,b\nc,d\ne,f\n", encoding="utf-8")
    with pytest.raises(COAParseError) as exc:
        parse_coa(bad)
    assert str(exc.value) == BAD_COA_MESSAGE


def test_hierarchy_split():
    result = parse_coa(COA_FULL)
    by_path = {a.full_path: a for a in result.accounts}
    rover = by_path["FIXED ASSETS:Vehicles:2019 Range Rover"]
    assert rover.leaf_name == "2019 Range Rover"
    assert rover.qbo_name == "FIXED ASSETS:Vehicles:2019 Range Rover"
    assert by_path["FIXED ASSETS:Vehicles"].leaf_name == "Vehicles"
    assert by_path["FIXED ASSETS"].leaf_name == "FIXED ASSETS"
    assert by_path["Checking"].leaf_name == "Checking"


def test_account_numbers_present_and_absent():
    by_path = {a.full_path: a for a in parse_coa(COA_FULL).accounts}
    assert by_path["Checking"].account_number == "10100"
    assert by_path["FIXED ASSETS:Vehicles:2019 Range Rover"].account_number == "15110"
    # Populated column, empty cell → None.
    assert by_path["Money Market (8556)"].account_number is None
    # No number column at all → all None, with a warning.
    minimal = parse_coa(COA_MINIMAL)
    assert all(a.account_number is None for a in minimal.accounts)
    assert any("account-number column" in w for w in minimal.warnings)


def test_total_balance_header_maps_to_balance():
    by_path = {a.full_path: a for a in parse_coa(COA_FULL).accounts}
    assert by_path["Money Market (8556)"].balance == pytest.approx(123424.21)
    assert by_path["Design Income"].balance == pytest.approx(-7000.00)
    minimal = {a.full_path: a for a in parse_coa(COA_MINIMAL).accounts}
    assert minimal["Job Materials"].balance == pytest.approx(2500.00)
    # "Balance" still accepted alongside "Total balance" (column present →
    # no warning).
    assert not any("balance column" in w for w in parse_coa(COA_FULL).warnings)


def test_empty_balance_cell_is_none():
    by_path = {a.full_path: a for a in parse_coa(COA_FULL).accounts}
    assert by_path["Repairs, Outside Services"].balance is None
    minimal = {a.full_path: a for a in parse_coa(COA_MINIMAL).accounts}
    assert minimal["Accounts Receivable"].balance is None


def test_missing_balance_column_warns(tmp_path):
    no_balance = tmp_path / "coa_no_balance.csv"
    no_balance.write_text(
        "Beta Builders LLC,,\n"
        "Account List,,\n"
        ",,\n"
        "Account,Type,Detail type\n"
        "Job Materials,Cost of Goods Sold,Supplies & materials - COGS\n",
        encoding="utf-8",
    )
    result = parse_coa(no_balance)
    assert all(a.balance is None for a in result.accounts)
    assert any("balance column" in w for w in result.warnings)


def test_description_captured():
    by_path = {a.full_path: a for a in parse_coa(COA_FULL).accounts}
    assert by_path["Checking"].description == "Main operating account"
    assert by_path["FIXED ASSETS:Vehicles:2019 Range Rover"].description == "Company vehicle"
    assert by_path["Money Market (8556)"].description is None
    minimal = {a.full_path: a for a in parse_coa(COA_MINIMAL).accounts}
    assert minimal["Job Materials"].description == "Job material purchases"


def test_comma_in_name_account_parses():
    by_path = {a.full_path: a for a in parse_coa(COA_FULL).accounts}
    repairs = by_path["Repairs, Outside Services"]
    assert repairs.qbo_type == "Expenses"
    assert repairs.detail_type == "Repairs & Maintenance"


def test_deleted_marker():
    result = parse_coa(COA_FULL)
    by_path = {a.full_path: a for a in result.accounts}
    assert by_path["Old Loan (deleted)"].deleted_marker is True
    assert sum(1 for a in result.accounts if a.deleted_marker) == 1
    assert all(not a.deleted_marker for a in parse_coa(COA_MINIMAL).accounts)


def test_total_row_excluded_in_both_variants():
    # Variant 2: TOTAL marker in the Account # cell, name cell empty.
    full = parse_coa(COA_FULL)
    totals = [e for e in full.excluded_rows if e["reason"] == "report total row"]
    assert len(totals) == 1
    assert totals[0]["raw"][0] == "TOTAL"
    # Variant 1: TOTAL marker in the name cell.
    minimal = parse_coa(COA_MINIMAL)
    totals = [e for e in minimal.excluded_rows
              if e["reason"] == "report total row"]
    assert len(totals) == 1
    assert totals[0]["raw"][0] == "TOTAL"
    # Never an account in either variant.
    for result in (full, minimal):
        assert all(a.full_path.lower() != "total" for a in result.accounts)


def test_row_reconciliation_and_footer():
    for path in (COA_FULL, COA_MINIMAL):
        result = parse_coa(path)
        assert sum(result.counts.values()) == result.row_count
        reasons = [e["reason"] for e in result.excluded_rows]
        assert "report footer" in reasons
        assert "blank row" in reasons
        assert "report total row" in reasons
        footer = next(e for e in result.excluded_rows
                      if e["reason"] == "report footer")
        assert footer["raw"][0].startswith(" Friday")


def test_account_counts():
    assert len(parse_coa(COA_FULL).accounts) == 8
    assert len(parse_coa(COA_MINIMAL).accounts) == 4


def test_missing_required_columns_raises(tmp_path):
    bad = tmp_path / "bad_coa.csv"
    bad.write_text(
        "Acme Services Inc,,\n"
        "Account List,,\n"
        ",,\n"
        "Full name,Detail type,Balance\n"
        "Checking,Checking,5.00\n",
        encoding="utf-8",
    )
    with pytest.raises(COAParseError):
        parse_coa(bad)


def test_unclassifiable_row_is_hard_error(tmp_path):
    broken = tmp_path / "coa_stray.csv"
    text = COA_FULL.read_text(encoding="utf-8")
    # An account row missing its Type cell (and not a TOTAL marker) can't
    # be classified.
    text = text.replace(
        "40100,Design Income,Income,Service/Fee Income",
        "40100,Design Income,,Service/Fee Income",
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(COAParseError) as exc:
        parse_coa(broken)
    assert "couldn't be classified" in str(exc.value)
