"""Tests for the General Ledger parser (core/parsers/gl.py)."""

from pathlib import Path

import pytest

from core.parsers.gl import (
    BAD_HEADER_MESSAGE,
    GLParseError,
    parse_amount,
    parse_gl,
)

FIXTURES = Path(__file__).parent / "fixtures"
GL_WITH_BB = FIXTURES / "gl_with_beginning_balances.csv"
GL_NO_BB = FIXTURES / "gl_no_beginning_balances.csv"
GL_UNLABELED_BB = FIXTURES / "gl_unlabeled_beginning_balances.csv"
GL_XLSX = FIXTURES / "gl_sample.xlsx"


def test_account_count_and_names():
    result = parse_gl(GL_WITH_BB)
    assert [a.name for a in result.accounts] == [
        "Checking",
        "Repairs, Outside Services",
        "Design Income",
    ]


def test_beginning_balances_read_from_rows():
    result = parse_gl(GL_WITH_BB)
    by_name = {a.name: a for a in result.accounts}
    assert by_name["Checking"].beginning_balance == pytest.approx(5000.00)
    assert by_name["Repairs, Outside Services"].beginning_balance == pytest.approx(0.0)
    assert by_name["Design Income"].beginning_balance == pytest.approx(0.0)


def test_beginning_balances_derived_when_missing():
    result = parse_gl(GL_NO_BB)
    by_name = {a.name: a for a in result.accounts}
    # First txn: amount 800.00, running balance 800.00 → derived beginning 0.
    assert by_name["Job Materials"].beginning_balance == pytest.approx(0.0)
    assert by_name["Consulting Income"].beginning_balance == pytest.approx(0.0)


def test_running_balance_gate_passes_clean_files():
    for path in (GL_WITH_BB, GL_NO_BB, GL_XLSX):
        result = parse_gl(path)
        assert result.accounts, path


def test_tampered_balance_raises_naming_account(tmp_path):
    corrupted = tmp_path / "gl_corrupt.csv"
    text = GL_WITH_BB.read_text(encoding="utf-8")
    assert '"8,349.75"' in text
    corrupted.write_text(text.replace('"8,349.75"', '"9,349.75"'), encoding="utf-8")
    with pytest.raises(GLParseError) as exc:
        parse_gl(corrupted)
    assert "Checking" in str(exc.value)
    assert "08/10/2025" in str(exc.value)
    assert "re-export the full date range" in str(exc.value)


def test_comma_in_account_name_keeps_all_rows():
    result = parse_gl(GL_WITH_BB)
    by_name = {a.name: a for a in result.accounts}
    account = by_name["Repairs, Outside Services"]
    assert len(account.transactions) == 4
    assert account.transactions[1].amount == pytest.approx(1234.56)
    # Row-count reconciliation: every input row classified exactly once.
    assert sum(result.counts.values()) == result.row_count


def test_job_prefix_extraction():
    result = parse_gl(GL_NO_BB)
    txns = {a.name: a.transactions for a in result.accounts}["Job Materials"]
    assert [(t.num, t.job_prefix) for t in txns] == [
        ("26013-0042", "26013"),
        ("26014-0001", "26014"),
        ("5512", None),
    ]
    # Non-numeric-prefixed nums never produce a prefix.
    design = {a.name: a for a in parse_gl(GL_WITH_BB).accounts}["Design Income"]
    assert design.transactions[2].num == "CM-3"
    assert design.transactions[2].job_prefix is None


def test_xlsx_produces_identical_results_to_csv():
    csv_result = parse_gl(GL_WITH_BB)
    xlsx_result = parse_gl(GL_XLSX)
    assert xlsx_result.accounts == csv_result.accounts
    assert xlsx_result.counts == csv_result.counts
    assert xlsx_result.period_start == csv_result.period_start
    assert xlsx_result.period_end == csv_result.period_end


def test_declared_net_activity_validations_pass_on_clean_files():
    for path in (GL_WITH_BB, GL_NO_BB, GL_UNLABELED_BB):
        result = parse_gl(path)
        for account in result.accounts:
            txn_sum = sum(t.amount for t in account.transactions)
            assert account.declared_net_activity == pytest.approx(
                txn_sum, abs=0.005
            ), account.name
            beginning = account.beginning_balance or 0.0
            assert beginning + account.declared_net_activity == pytest.approx(
                account.ending_balance, abs=0.005
            ), account.name
        assert not [w for w in result.warnings if "doesn't match" in w]


def test_net_activity_distinct_from_ending_balance():
    # Sections with nonzero beginnings: Total row ≠ ending balance.
    checking = {a.name: a for a in parse_gl(GL_WITH_BB).accounts}["Checking"]
    assert checking.declared_net_activity == pytest.approx(2150.00)
    assert checking.ending_balance == pytest.approx(7150.00)

    mm = {a.name: a for a in parse_gl(GL_UNLABELED_BB).accounts}["Money Market (8556)"]
    assert mm.beginning_balance_source == "unlabeled"
    assert mm.declared_net_activity == pytest.approx(-24797.35)
    assert mm.ending_balance == pytest.approx(0.0)


def test_beginning_plus_activity_mismatch_is_warning_not_error(tmp_path):
    modified = tmp_path / "gl_activity_off.csv"
    text = GL_WITH_BB.read_text(encoding="utf-8")
    modified.write_text(text.replace('"$2,150.00"', '"$2,151.00"'), encoding="utf-8")
    result = parse_gl(modified)
    assert any(
        "plus net activity" in w and "Checking" in w for w in result.warnings
    )


def test_activity_vs_transaction_sum_mismatch_is_warning_not_error(tmp_path):
    modified = tmp_path / "gl_sum_off.csv"
    text = GL_WITH_BB.read_text(encoding="utf-8")
    modified.write_text(text.replace('"$2,150.00"', '"$2,151.00"'), encoding="utf-8")
    result = parse_gl(modified)
    assert any(
        "sum of parsed transactions" in w and "Checking" in w
        for w in result.warnings
    )


def test_zero_transaction_section_handled():
    result = parse_gl(GL_NO_BB)
    by_name = {a.name: a for a in result.accounts}
    rental = by_name["Equipment Rental"]
    assert rental.transactions == []
    assert rental.beginning_balance == pytest.approx(0.0)
    assert rental.ending_balance == pytest.approx(0.0)
    assert any("Equipment Rental" in w for w in result.warnings)


def test_footer_landed_in_excluded_rows():
    for path in (GL_WITH_BB, GL_NO_BB):
        result = parse_gl(path)
        reasons = [e["reason"] for e in result.excluded_rows]
        assert "report footer" in reasons
        footer = next(e for e in result.excluded_rows if e["reason"] == "report footer")
        assert footer["raw"][0].startswith("Accrual Basis")


def test_period_carried_from_detection():
    result = parse_gl(GL_WITH_BB)
    assert result.period_start == "2025-06-01"
    assert result.period_end == "2026-05-31"


def test_bad_header_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "Acme Services Inc,,,\n"
        "General Ledger,,,\n"
        '"June, 2025-May, 2026",,,\n'
        ",,,\n"
        "Foo,Bar,Baz,Qux\n",
        encoding="utf-8",
    )
    with pytest.raises(GLParseError) as exc:
        parse_gl(bad)
    assert str(exc.value) == BAD_HEADER_MESSAGE


def test_unaccounted_row_is_hard_error(tmp_path):
    broken = tmp_path / "gl_stray_row.csv"
    text = GL_WITH_BB.read_text(encoding="utf-8")
    # Inject a row with empty col A whose col B is neither a date nor
    # "Beginning Balance" — must not be silently dropped.
    text = text.replace(
        "Total for Checking",
        ',not a date,mystery,,,,,,42.00\nTotal for Checking',
        1,
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(GLParseError) as exc:
        parse_gl(broken)
    assert "couldn't be classified" in str(exc.value)


def test_unlabeled_beginning_balance_read():
    result = parse_gl(GL_UNLABELED_BB)
    by_name = {a.name: a for a in result.accounts}
    mm = by_name["Money Market (8556)"]
    assert mm.beginning_balance == pytest.approx(24797.35)
    assert mm.beginning_balance_source == "unlabeled"
    assert len(mm.transactions) == 2
    # Running-balance gate passed through the unlabeled beginning:
    # 24,797.35 - 5,000.00 = 19,797.35; 19,797.35 - 19,797.35 = 0.00.
    assert mm.ending_balance == pytest.approx(0.0)
    assert mm.declared_net_activity == pytest.approx(-24797.35)
    # Both rows counted under beginning_balance in the reconciliation.
    assert result.counts["beginning_balance"] == 2
    assert sum(result.counts.values()) == result.row_count


def test_unlabeled_negative_beginning_balance():
    result = parse_gl(GL_UNLABELED_BB)
    cc = {a.name: a for a in result.accounts}["Credit Card Payable"]
    assert cc.beginning_balance == pytest.approx(-1250.00)
    assert cc.beginning_balance_source == "unlabeled"
    assert cc.ending_balance == pytest.approx(-550.00)
    assert cc.declared_net_activity == pytest.approx(700.00)


def test_tampered_balance_after_unlabeled_beginning_raises(tmp_path):
    corrupted = tmp_path / "gl_unlabeled_corrupt.csv"
    text = GL_UNLABELED_BB.read_text(encoding="utf-8")
    assert '"19,797.35"' in text
    corrupted.write_text(
        text.replace(',"-5,000.00","19,797.35"', ',"-5,000.00","18,797.35"'),
        encoding="utf-8",
    )
    with pytest.raises(GLParseError) as exc:
        parse_gl(corrupted)
    assert "Money Market (8556)" in str(exc.value)
    assert "06/02/2025" in str(exc.value)


def test_beginning_balance_source_all_three_paths():
    labeled = {a.name: a for a in parse_gl(GL_WITH_BB).accounts}
    assert labeled["Checking"].beginning_balance_source == "labeled"

    derived = {a.name: a for a in parse_gl(GL_NO_BB).accounts}
    assert derived["Job Materials"].beginning_balance_source == "derived"
    # Zero-transaction section has no beginning row to read or derive from.
    assert derived["Equipment Rental"].beginning_balance_source is None

    unlabeled = {a.name: a for a in parse_gl(GL_UNLABELED_BB).accounts}
    assert unlabeled["Money Market (8556)"].beginning_balance_source == "unlabeled"


def test_balance_only_row_mid_section_excluded_with_warning(tmp_path):
    modified = tmp_path / "gl_mid_section_balance.csv"
    text = GL_WITH_BB.read_text(encoding="utf-8")
    # Inject a balance-only row between Checking's transactions.
    text = text.replace(
        ',09/01/2025,Check,1042',
        ',,,,,,,,"99,999.99"\n,09/01/2025,Check,1042',
        1,
    )
    modified.write_text(text, encoding="utf-8")
    result = parse_gl(modified)
    excluded = [
        e for e in result.excluded_rows
        if e["reason"] == "balance-only row mid-section"
    ]
    assert len(excluded) == 1
    assert excluded[0]["raw"][8] == "99,999.99"
    assert any(
        "balance-only row" in w and "Checking" in w for w in result.warnings
    )
    # The section still parsed completely around the skipped row.
    checking = {a.name: a for a in result.accounts}["Checking"]
    assert len(checking.transactions) == 4
    assert checking.ending_balance == pytest.approx(7150.00)
    assert sum(result.counts.values()) == result.row_count


def test_dollar_prefix_inside_transaction_cells():
    result = parse_gl(GL_UNLABELED_BB)
    by_name = {a.name: a for a in result.accounts}
    # "$0.00" in a transaction's Balance cell.
    assert by_name["Money Market (8556)"].transactions[-1].running_balance == pytest.approx(0.0)
    # "$-550.00" in a transaction's Balance cell.
    assert by_name["Credit Card Payable"].transactions[-1].running_balance == pytest.approx(-550.0)


def test_zero_amount_rows_parse_as_transactions():
    result = parse_gl(GL_NO_BB)
    ar = {a.name: a for a in result.accounts}["Accounts Receivable"]
    assert len(ar.transactions) == 4
    payment = ar.transactions[2]
    assert payment.txn_type == "Payment"
    assert payment.amount == 0.0
    assert payment.running_balance == pytest.approx(496690.86)
    memo_je = ar.transactions[3]
    assert memo_je.txn_type == "Journal Entry"
    assert memo_je.amount == 0.0
    # Counted as transactions in row reconciliation.
    assert result.counts["transaction"] >= 4
    assert sum(result.counts.values()) == result.row_count


def test_zero_amount_rows_pass_running_balance_gate():
    result = parse_gl(GL_NO_BB)
    ar = {a.name: a for a in result.accounts}["Accounts Receivable"]
    assert ar.beginning_balance == pytest.approx(496690.86)
    assert ar.ending_balance == pytest.approx(496690.86)
    assert ar.declared_net_activity == pytest.approx(0.0)


def test_tampered_balance_on_zero_amount_row_raises(tmp_path):
    corrupted = tmp_path / "gl_zero_amount_corrupt.csv"
    text = GL_NO_BB.read_text(encoding="utf-8")
    target = ',06/20/2025,Payment,,Carolina Perez:Carolina Coffee Table,,,,"496,690.86"'
    assert target in text
    corrupted.write_text(
        text.replace(target, target.replace("496,690.86", "496,000.00")),
        encoding="utf-8",
    )
    with pytest.raises(GLParseError) as exc:
        parse_gl(corrupted)
    assert "Accounts Receivable" in str(exc.value)
    assert "06/20/2025" in str(exc.value)


def test_zero_amount_count_and_summary_warning():
    result = parse_gl(GL_NO_BB)
    assert result.zero_amount_count == 2
    assert any(
        "2 zero-amount transactions" in w and "empty Amount" in w
        for w in result.warnings
    )
    # Files without zero-amount rows report none and stay quiet.
    clean = parse_gl(GL_WITH_BB)
    assert clean.zero_amount_count == 0
    assert not any("zero-amount" in w for w in clean.warnings)


def test_empty_balance_on_transaction_row_is_hard_error(tmp_path):
    broken = tmp_path / "gl_no_balance.csv"
    text = GL_WITH_BB.read_text(encoding="utf-8")
    target = ',08/10/2025,Deposit,,Customer B,Progress billing,Accounts Receivable,"2,500.00","8,349.75"'
    assert target in text
    broken.write_text(
        text.replace(target, target.replace(',"8,349.75"', ","), 1),
        encoding="utf-8",
    )
    with pytest.raises(GLParseError) as exc:
        parse_gl(broken)
    assert "no Balance value" in str(exc.value)
    assert "Checking" in str(exc.value)


def test_parse_amount_forms():
    assert parse_amount('"1,234.56"') == pytest.approx(1234.56)
    assert parse_amount("$-7,000.00") == pytest.approx(-7000.0)
    assert parse_amount("(1,234.56)") == pytest.approx(-1234.56)
    assert parse_amount("") is None
    assert parse_amount(None) is None
