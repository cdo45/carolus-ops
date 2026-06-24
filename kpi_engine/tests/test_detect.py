"""Tests for report-type detection (core/detect.py)."""

from pathlib import Path

from core.detect import TB_UNSUPPORTED_MESSAGE, detect_report_type, read_rows

FIXTURES = Path(__file__).parent / "fixtures"


def test_gl_csv_detects_with_comma_period_style():
    result = detect_report_type(FIXTURES / "gl_with_beginning_balances.csv")
    assert result.report_type == "GL"
    assert result.company_name == "Acme Services Inc"
    assert result.period_start == "2025-06-01"
    assert result.period_end == "2026-05-31"
    assert result.as_of_date is None
    assert result.header_row_index == 4
    assert result.message is None


def test_gl_csv_detects_with_spaced_period_style():
    result = detect_report_type(FIXTURES / "gl_no_beginning_balances.csv")
    assert result.report_type == "GL"
    assert result.company_name == "Beta Builders LLC"
    assert result.period_start == "2025-06-01"
    assert result.period_end == "2026-05-31"
    assert result.header_row_index == 4


def test_gl_xlsx_detects_same_as_csv():
    csv_result = detect_report_type(FIXTURES / "gl_with_beginning_balances.csv")
    xlsx_result = detect_report_type(FIXTURES / "gl_sample.xlsx")
    assert xlsx_result == csv_result


def test_xlsx_rows_match_csv_rows():
    csv_rows = read_rows(FIXTURES / "gl_with_beginning_balances.csv")
    xlsx_rows = read_rows(FIXTURES / "gl_sample.xlsx")
    assert xlsx_rows == csv_rows


def test_trial_balance_flagged_unsupported():
    result = detect_report_type(FIXTURES / "trial_balance.csv")
    assert result.report_type == "TB"
    assert result.as_of_date == "2026-05-31"
    assert result.period_start is None
    assert result.message == TB_UNSUPPORTED_MESSAGE


def test_unknown_carries_title_text():
    result = detect_report_type(FIXTURES / "not_qbo.csv")
    assert result.report_type == "UNKNOWN"
    assert result.message is None
    assert result.title_text == "1,foo,10"


def test_other_report_titles(tmp_path):
    cases = [
        ("Chart of Accounts", "COA"),
        ("Account List", "COA"),
        ("A/R Aging Detail", "AR_AGING"),
        ("A/P Aging Detail", "AP_AGING"),
        ("Invoices and Received Payments", "INVOICES_PAYMENTS"),
        ("Bills and Applied Payments", "BILLS_PAYMENTS"),
    ]
    for title, expected in cases:
        p = tmp_path / "report.csv"
        p.write_text(
            "Acme Services Inc,,\n"
            f"{title},,\n"
            '"As of May 31, 2026",,\n'
            ",,\n"
            "ColA,ColB,ColC\n",
            encoding="utf-8",
        )
        result = detect_report_type(p)
        assert result.report_type == expected, title
        assert result.as_of_date == "2026-05-31"


def test_utf8_bom_handled(tmp_path):
    p = tmp_path / "gl_bom.csv"
    p.write_text(
        "Acme Services Inc,,\n"
        "General Ledger,,\n"
        '"June, 2025-May, 2026",,\n',
        encoding="utf-8-sig",
    )
    result = detect_report_type(p)
    assert result.report_type == "GL"
    assert result.company_name == "Acme Services Inc"
