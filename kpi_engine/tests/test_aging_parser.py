"""Tests for the AR/AP Aging Detail parser (core/parsers/aging.py)."""

from pathlib import Path

import pytest

from core.detect import detect_report_type
from core.parsers.aging import AgingParseError, parse_aging

FIXTURES = Path(__file__).parent / "fixtures"
AR_AGING = FIXTURES / "ar_aging_detail.csv"
AP_AGING = FIXTURES / "ap_aging_detail.csv"


def test_fixtures_detect_with_abbreviated_as_of():
    ar = detect_report_type(AR_AGING)
    assert ar.report_type == "AR_AGING"
    assert ar.as_of_date == "2026-06-11"
    ap = detect_report_type(AP_AGING)
    assert ap.report_type == "AP_AGING"
    assert ap.as_of_date == "2026-06-11"


def test_ar_shape_and_buckets():
    result = parse_aging(AR_AGING)
    assert result.side == "AR"
    assert result.as_of_date == "2026-06-11"
    assert [b.name for b in result.buckets] == [
        "91 or more days past due",
        "61 - 90 days past due",
        "31 - 60 days past due",
        "1 - 30 days past due",
        "CURRENT",
    ]
    assert result.grand_total == pytest.approx(4600.00)
    # AR has no Past due column.
    assert all(
        r.past_due_days is None for b in result.buckets for r in b.rows
    )


def test_ap_past_due_days_including_negative():
    result = parse_aging(AP_AGING)
    assert result.side == "AP"
    current = next(b for b in result.buckets if b.name == "CURRENT")
    assert [r.past_due_days for r in current.rows] == [-19, -23]
    oldest = next(
        b for b in result.buckets if b.name == "91 or more days past due"
    )
    assert oldest.rows[0].past_due_days == 327
    assert oldest.rows[0].party == "Joe's Welding"


def test_bucket_totals_tie_and_grand_total_ties():
    for path in (AR_AGING, AP_AGING):
        result = parse_aging(path)
        assert not result.warnings, result.warnings
        for bucket in result.buckets:
            row_sum = sum(
                r.open_balance for r in bucket.rows
                if r.open_balance is not None
            )
            assert row_sum == pytest.approx(bucket.declared_total, abs=0.005)
        declared_sum = sum(b.declared_total for b in result.buckets)
        assert declared_sum == pytest.approx(result.grand_total, abs=0.005)


def test_bucket_mismatch_is_warning(tmp_path):
    modified = tmp_path / "ar_off.csv"
    text = AR_AGING.read_text(encoding="utf-8")
    modified.write_text(
        text.replace("Total for 61 - 90 days past due,,,,,,,500.00",
                     "Total for 61 - 90 days past due,,,,,,,501.00"),
        encoding="utf-8",
    )
    result = parse_aging(modified)
    assert any("'61 - 90 days past due'" in w and "501.00" in w
               for w in result.warnings)
    # The grand-total tie now breaks too.
    assert any("grand" in w for w in result.warnings)


def test_empty_vs_zero_open_balance():
    result = parse_aging(AR_AGING)
    current = next(b for b in result.buckets if b.name == "CURRENT")
    assert current.rows[0].open_balance is None  # empty cell
    assert current.rows[1].open_balance == 0.0 or current.rows[1].open_balance == pytest.approx(450.0)
    one_thirty = next(b for b in result.buckets if b.name == "1 - 30 days past due")
    assert one_thirty.rows[0].open_balance == 0.0  # explicit "0.00"


def test_leading_space_timestamp_footer_excluded():
    for path in (AR_AGING, AP_AGING):
        result = parse_aging(path)
        footer = [e for e in result.excluded_rows
                  if e["reason"] == "report footer"]
        assert len(footer) == 1
        assert footer[0]["raw"][0].startswith(" Friday")


def test_row_reconciliation():
    for path in (AR_AGING, AP_AGING):
        result = parse_aging(path)
        assert sum(result.counts.values()) == result.row_count
        assert result.counts["grand_total"] == 1


def test_dates_parsed_iso():
    result = parse_aging(AR_AGING)
    first = result.buckets[0].rows[0]
    assert first.txn_date == "2025-06-18"
    assert first.due_date == "2025-07-18"
    assert first.amount == pytest.approx(3000.00)
    assert first.open_balance == pytest.approx(1200.00)


def test_total_row_not_a_bucket():
    result = parse_aging(AR_AGING)
    assert all(b.name != "TOTAL" for b in result.buckets)


def test_unclassifiable_row_is_hard_error(tmp_path):
    broken = tmp_path / "ar_stray.csv"
    text = AR_AGING.read_text(encoding="utf-8")
    text = text.replace(
        "TOTAL,,,,,,,",
        ",not a date,Mystery,,,,42.00,\nTOTAL,,,,,,,",
        1,
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(AgingParseError) as exc:
        parse_aging(broken)
    assert "couldn't be classified" in str(exc.value)


def test_missing_required_columns_raises(tmp_path):
    bad = tmp_path / "bad_aging.csv"
    bad.write_text(
        "Acme,,\nA/R Aging Detail,,\n\"As of Jun 11, 2026\",,\n,,\n"
        "Foo,Bar,Baz\n",
        encoding="utf-8",
    )
    with pytest.raises(AgingParseError):
        parse_aging(bad)
