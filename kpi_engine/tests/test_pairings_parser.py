"""Tests for the invoices/bills pairing parser (core/parsers/pairings.py)."""

from pathlib import Path

import pytest

from core.detect import detect_report_type
from core.parsers.pairings import (
    PairingsParseError,
    classify_row_type,
    parse_pairings,
)

FIXTURES = Path(__file__).parent / "fixtures"
INVOICES = FIXTURES / "invoices_payments.csv"
BILLS = FIXTURES / "bills_payments.csv"


def test_fixtures_detect():
    assert detect_report_type(INVOICES).report_type == "INVOICES_PAYMENTS"
    assert detect_report_type(BILLS).report_type == "BILLS_PAYMENTS"


def test_header_adaptivity_across_column_orders():
    ar = parse_pairings(INVOICES)
    ap = parse_pairings(BILLS)
    assert ar.side == "AR" and ap.side == "AP"
    # AR: memo before num; AP: num before memo — both must land correctly.
    ar_row = ar.parties[0].rows[1]
    assert (ar_row.memo, ar_row.num) == ("Coffee table", "1056")
    ap_row = ap.parties[0].rows[1]
    assert (ap_row.num, ap_row.memo) == ("26013-0042", "Fasteners")
    assert (ar.period_start, ar.period_end) == ("2025-06-01", "2026-05-31")


def test_row_classification():
    assert classify_row_type("Invoice") == "invoice"
    assert classify_row_type("Bill") == "invoice"
    assert classify_row_type("Payment") == "payment"
    assert classify_row_type("Bill Payment (Credit Card)") == "payment"
    assert classify_row_type("Bill Payment (Check)") == "payment"
    assert classify_row_type("Vendor Credit") == "credit"
    assert classify_row_type("Journal Entry") == "other"
    assert classify_row_type("Deposit") == "other"
    ap = parse_pairings(BILLS)
    card = next(p for p in ap.parties if p.name == "Card Services")
    assert [r.row_type for r in card.rows] == [
        "payment", "credit", "other", "other", "invoice",
    ]


def test_payment_covering_three_invoices_clusters_together():
    ar = parse_pairings(INVOICES)
    carolina = next(p for p in ar.parties if p.name == "Carolina Perez")
    assert carolina.rows[0].row_type == "payment"
    assert carolina.rows[0].amount == pytest.approx(4368.00)
    assert carolina.rows[0].num is None  # payment with empty transaction number
    invoices = carolina.rows[1:]
    assert [i.amount for i in invoices] == [pytest.approx(1456.00)] * 3
    assert len({r.group_key for r in carolina.rows}) == 1
    assert carolina.rows[0].amount == pytest.approx(
        sum(i.amount for i in invoices)
    )


def test_multibill_payment_cluster_sums():
    ap = parse_pairings(BILLS)
    hardware = next(p for p in ap.parties if p.name == "Hardware Hut")
    payment = hardware.rows[0]
    assert payment.row_type == "payment"
    assert payment.amount == pytest.approx(-336.84)
    bills = hardware.rows[1:]
    assert len(bills) == 6
    assert sum(b.amount for b in bills) == pytest.approx(336.84)
    assert len({r.group_key for r in hardware.rows}) == 1


def test_unpaid_partial_invoice():
    ar = parse_pairings(INVOICES)
    customer_b = next(p for p in ar.parties if p.name == "Customer B")
    row = customer_b.rows[0]
    assert row.paid_status == "Unpaid"
    assert row.amount == pytest.approx(7510.00)
    assert row.open_balance == pytest.approx(61.85)
    # Pre-payment invoice gets its own cluster.
    assert row.group_key == "Customer B::1"


def test_pre_payment_invoices_own_cluster_then_attach():
    ar = parse_pairings(INVOICES)
    customer_c = next(p for p in ar.parties if p.name == "Customer C")
    keys = [r.group_key for r in customer_c.rows]
    # invoice (own cluster), payment (new cluster), invoice (attaches).
    assert keys == ["Customer C::1", "Customer C::2", "Customer C::2"]


def test_empty_vs_zero_open_balance():
    ar = parse_pairings(INVOICES)
    carolina = next(p for p in ar.parties if p.name == "Carolina Perez")
    assert carolina.rows[0].open_balance == 0.0  # explicit "0.00"
    assert carolina.rows[1].open_balance is None  # empty cell
    assert carolina.rows[2].open_balance == 0.0


def test_paid_status_none_on_ap():
    ap = parse_pairings(BILLS)
    assert all(r.paid_status is None for p in ap.parties for r in p.rows)


def test_job_prefix_extraction():
    ap = parse_pairings(BILLS)
    hardware = next(p for p in ap.parties if p.name == "Hardware Hut")
    prefixes = [r.job_prefix for r in hardware.rows]
    assert prefixes == [None, "26013", "26013", "26013", "26014", "26014", None]


def test_footer_and_reconciliation():
    for path in (INVOICES, BILLS):
        result = parse_pairings(path)
        footer = [e for e in result.excluded_rows
                  if e["reason"] == "report footer"]
        assert len(footer) == 1
        assert footer[0]["raw"][0].startswith(" Friday")
        assert sum(result.counts.values()) == result.row_count


def test_unclassifiable_row_is_hard_error(tmp_path):
    broken = tmp_path / "ip_stray.csv"
    text = INVOICES.read_text(encoding="utf-8")
    text = text.replace(
        "Customer B,,,,,,,",
        ",garbage,Mystery,,,,,\nCustomer B,,,,,,,",
        1,
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(PairingsParseError) as exc:
        parse_pairings(broken)
    assert "couldn't be classified" in str(exc.value)
