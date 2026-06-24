"""CHUNK 7 tests: the 13-week cashflow forecast, persistence, and variance.

CLIENT (forecast), anchor = 2026-03-27 (latest cash transaction):
  Checking beginning 50000; outflows 01..03/2026 ->
    payroll  7 x 1000 weekly  (Fridays 02-13 .. 03-27)   = 7000
    rent     3 x 2000 monthly (the 1st)                  = 6000   OH-OCC
    software 3 x  400 monthly (the 2nd)                  = 1200   OH (open)
    draws    3 x 1500 monthly (the 5th)                  = 4500   EQ-DRAW
  beginning_cash = 50000 - 18700 = 31300

  Projected forward onto the week grid (week 1 = 03-28 .. 04-03):
    collections: INV-A 1000 @lag14 -> 03-31 (wk1); INV-B 800 -> 04-07 (wk2)
    payroll:     04-03 -> wk1 1000
    recurring:   rent 04-01 (wk1) 2000 + software 04-02 (wk1) 400 = 2400
    taxes:       sales-tax 500 at next month-end 03-31 -> wk1 500
    owner_draws: first draw 04-05 -> wk2 (none in wk1)
    ap/card:     bill due 04-08 -> wk2; card payments start 04-15 -> wk3
  net wk1 = 1000 - (1000 + 2400 + 500) = -2900 ; ending wk1 = 31300 - 2900 = 28400
"""

import datetime as dt

import pytest

from core import db
from core.classify import classify_client
from core.importer import (
    dormancy_pass,
    import_aging,
    import_coa,
    import_gl,
    import_pairings,
)
from core.kpi.forecast import (
    compute_forecast,
    compute_variance,
    save_forecast,
)
from core.parsers.aging import parse_aging
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from core.parsers.pairings import parse_pairings
from tests.test_importer import write_coa, write_gl
from tests.test_kpi_b import write_ap_aging, write_ar_aging, write_ar_pairings

COA = [
    ("", "Checking", "Bank", "Checking", 31300.00),
    ("", "Accounts Receivable (A/R)", "Accounts receivable (A/R)",
     "Accounts Receivable", 0.00),
    ("", "Visa Card", "Credit Card", "Credit Card", 900.00),
    ("", "Office Payroll", "Expenses", "Payroll expenses", 0.00),
    ("", "Rent", "Expenses", "Rent or lease of buildings", 0.00),
    ("", "Software", "Expenses", "Dues & subscriptions", 0.00),
    ("", "Owner Draws", "Equity", "Partner Distributions", 0.00),
    ("", "Sales Tax Payable", "Other Current Liabilities",
     "Sales Tax Payable", 500.00),
]

PAYROLL_DATES = [
    "02/13/2026", "02/20/2026", "02/27/2026", "03/06/2026",
    "03/13/2026", "03/20/2026", "03/27/2026",
]

CHECKING_TXNS = (
    [(d, "Expense", "", "Gusto Payroll", "Payroll", "Office Payroll", -1000.00)
     for d in PAYROLL_DATES]
    + [("01/01/2026", "Expense", "", "City Properties", "Rent", "Rent",
        -2000.00),
       ("02/01/2026", "Expense", "", "City Properties", "Rent", "Rent",
        -2000.00),
       ("03/01/2026", "Expense", "", "City Properties", "Rent", "Rent",
        -2000.00)]
    + [("01/02/2026", "Expense", "", "CloudSoft", "SaaS", "Software", -400.00),
       ("02/02/2026", "Expense", "", "CloudSoft", "SaaS", "Software", -400.00),
       ("03/02/2026", "Expense", "", "CloudSoft", "SaaS", "Software", -400.00)]
    + [("01/05/2026", "Check", "", "Owner Draw", "Monthly draw", "Owner Draws",
        -1500.00),
       ("02/05/2026", "Check", "", "Owner Draw", "Monthly draw", "Owner Draws",
        -1500.00),
       ("03/05/2026", "Check", "", "Owner Draw", "Monthly draw", "Owner Draws",
        -1500.00)]
)

GL = [
    {"name": "Checking", "beginning": 50000.00, "txns": CHECKING_TXNS},
    {"name": "Visa Card", "beginning": 1000.00, "txns": [
        ("01/10/2026", "Credit Card Charge", "", "Supplier", "Materials",
         "Job Materials", 500.00),
        ("01/15/2026", "Credit Card Payment", "", "Visa", "Payment",
         "Checking", -300.00),
        ("02/15/2026", "Credit Card Payment", "", "Visa", "Payment",
         "Checking", -300.00),
    ]},
    {"name": "Sales Tax Payable", "beginning": 0.00, "txns": [
        ("03/10/2026", "Journal Entry", "", "", "Sales tax accrual",
         "Sales Tax Expense", 500.00),
    ]},
]


@pytest.fixture
def client(tmp_path):
    conn = db.get_client_db("fcast", base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    import_coa(conn, parse_coa(write_coa(tmp_path / "coa.csv", COA)))
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl.csv", GL,
              period="January, 2026-March, 2026")), "gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)

    # Three matched invoice/payment pairs at a 14-day lag set the client
    # median lag; the open invoices' customers inherit it.
    import_pairings(conn, parse_pairings(write_ar_pairings(
        tmp_path / "ar_pair.csv",
        [("Payer", [
            ("10/15/2025", "Payment", "", "", 500.00),
            ("10/01/2025", "Invoice", "", "P1", 500.00),
            ("11/15/2025", "Payment", "", "", 600.00),
            ("11/01/2025", "Invoice", "", "P2", 600.00),
            ("12/15/2025", "Payment", "", "", 700.00),
            ("12/01/2025", "Invoice", "", "P3", 700.00),
        ])],
    )), "ar_pair.csv")

    import_aging(conn, parse_aging(write_ar_aging(
        tmp_path / "ar_aging.csv", "As of Mar 27, 2026",
        [("CURRENT", [
            ("03/17/2026", "Invoice", "INV-A", "Gamma", "04/16/2026",
             1000.00, 1000.00),
            ("03/24/2026", "Invoice", "INV-B", "Delta", "04/23/2026",
             800.00, 800.00),
        ])],
    )), "ar_aging.csv")

    import_aging(conn, parse_aging(write_ap_aging(
        tmp_path / "ap_aging.csv", "As of Mar 27, 2026",
        [("1 - 30 days past due", [
            ("03/15/2026", "Bill", "B1", "BillVendor", "04/08/2026", 12,
             700.00, 700.00),
        ])],
    )), "ap_aging.csv")

    yield conn, global_conn, tmp_path
    conn.close()
    global_conn.close()


def _set_config(conn, key, value):
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def test_anchor_and_beginning_cash(client):
    conn, _, _ = client
    result = compute_forecast(conn, "BASE")
    assert result.anchor_date == "2026-03-27"
    assert result.beginning_cash == pytest.approx(31300.00, abs=0.01)
    assert len(result.weeks) == 13


def test_forecast_captures_drivers_for_explanations(client):
    conn, _, _ = client
    result = compute_forecast(conn, "BASE")
    drivers = result.drivers
    # Every line family records why its numbers are what they are.
    for fam in ("collections", "new_billings", "payroll", "recurring",
                "ap_scheduled", "card_payments", "taxes", "owner_draws"):
        assert fam in drivers
    # The detected weekly payroll surfaces its cadence and amount.
    payroll = drivers["payroll"]
    assert payroll and payroll[0]["cadence"] == "weekly"
    assert payroll[0]["typical_amount"] == pytest.approx(1000.00, abs=0.01)
    # Recurring carries per-item detail with the scenario's discretionary cut.
    assert "items" in drivers["recurring"]
    assert drivers["recurring"]["discretionary"] == 1.0
    # Collections cite each open invoice — customer, number, and the
    # days-to-pay used to time it.
    invoices = drivers["collections"]["invoice_list"]
    assert {i["customer"] for i in invoices} >= {"Gamma", "Delta"}
    sample = next(i for i in invoices if i["customer"] == "Gamma")
    assert sample["invoice"] == "INV-A"
    assert "days_to_pay" in sample and "lag_basis" in sample


def test_week1_components_and_ending_cash(client):
    conn, _, _ = client
    week1 = compute_forecast(conn, "BASE").weeks[0]
    assert week1.start_date == "2026-03-28"
    assert week1.end_date == "2026-04-03"
    assert week1.inflows["collections"] == pytest.approx(1000.00, abs=0.01)
    assert week1.outflows["payroll"] == pytest.approx(1000.00, abs=0.01)
    assert week1.outflows["recurring"] == pytest.approx(2400.00, abs=0.01)
    assert week1.outflows["taxes"] == pytest.approx(500.00, abs=0.01)
    assert week1.outflows["owner_draws"] == pytest.approx(0.00, abs=0.01)
    assert week1.net == pytest.approx(-2900.00, abs=0.01)
    assert week1.ending_cash == pytest.approx(28400.00, abs=0.01)
    assert week1.confidence_tier["payroll"] == "scheduled"
    assert week1.confidence_tier["collections"] == "behavioral"


def test_collections_land_in_expected_weeks(client):
    conn, _, _ = client
    weeks = compute_forecast(conn, "BASE").weeks
    assert weeks[0].inflows["collections"] == pytest.approx(1000.00, abs=0.01)
    assert weeks[1].inflows["collections"] == pytest.approx(800.00, abs=0.01)


def test_owner_draw_lands_in_week2(client):
    conn, _, _ = client
    weeks = compute_forecast(conn, "BASE").weeks
    assert weeks[1].outflows["owner_draws"] == pytest.approx(1500.00, abs=0.01)


def test_stretch_pulls_a_collection_one_week_earlier(client):
    conn, _, _ = client
    weeks = compute_forecast(conn, "STRETCH").weeks
    # INV-B's 800 moves from week 2 into week 1; week 2 collections empty.
    assert weeks[0].inflows["collections"] == pytest.approx(1800.00, abs=0.01)
    assert weeks[1].inflows["collections"] == pytest.approx(0.00, abs=0.01)


def test_crunch_zeros_draws_and_cuts_discretionary_but_spares_rent(client):
    conn, _, _ = client
    result = compute_forecast(conn, "CRUNCH")
    assert all(w.outflows["owner_draws"] == 0.0 for w in result.weeks)
    assert any("owner draws paused" in n for n in result.notes)
    # Week 1 recurring: rent 2000 protected + software 400*0.75 = 2300.
    assert result.weeks[0].outflows["recurring"] == pytest.approx(2300.00,
                                                                  abs=0.01)


def test_breach_weeks_fire_with_high_floor(client):
    conn, _, _ = client
    assert compute_forecast(conn, "BASE").breach_weeks == []
    _set_config(conn, "cash_floor", 29000)
    result = compute_forecast(conn, "BASE")
    assert result.floor == pytest.approx(29000.00)
    # Ending cash dips to 28400 in week 1 already.
    assert 1 in result.breach_weeks


def test_scenario_param_override_via_config(client):
    conn, _, _ = client
    _set_config(conn, "scenario.BASE.discretionary", 0.5)
    week1 = compute_forecast(conn, "BASE").weeks[0]
    # Software (open) now halved: rent 2000 + 400*0.5 = 2200.
    assert week1.outflows["recurring"] == pytest.approx(2200.00, abs=0.01)


def test_save_forecast_persists_rows(client):
    conn, _, _ = client
    result = compute_forecast(conn, "BASE")
    run_id = save_forecast(conn, result)
    runs = conn.execute("SELECT scenario, run_date FROM forecast_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["scenario"] == "BASE"
    assert runs[0]["run_date"] == "2026-03-27"
    # 13 weeks x 9 row families.
    n = conn.execute(
        "SELECT COUNT(*) FROM forecast_rows WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    assert n == 13 * 9


def test_variance_empty_when_week_not_elapsed(client):
    conn, _, _ = client
    save_forecast(conn, compute_forecast(conn, "BASE"))
    # The run is anchored at the latest actual; week 1 hasn't elapsed yet.
    assert compute_variance(conn) == []


# ── variance round-trip on a staged client ───────────────────────────────────

def _variance_client(tmp_path):
    conn = db.get_client_db("vclient", base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    coa = [("", "Checking", "Bank", "Checking", 10000.00)]
    import_coa(conn, parse_coa(write_coa(tmp_path / "v_coa.csv", coa)))
    # Stage 1: cash activity through 01-30 -> anchor 2026-01-30.
    stage1 = [{"name": "Checking", "beginning": 10000.00, "txns": [
        ("01/02/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Checking", -1000.00),
        ("01/09/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Checking", -1000.00),
        ("01/16/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Checking", -1000.00),
        ("01/23/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Checking", -1000.00),
        ("01/30/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Checking", -1000.00),
    ]}]
    import_gl(conn, parse_gl(write_gl(tmp_path / "v_gl1.csv", stage1,
              period="January, 2026-January, 2026")), "v_gl1.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    return conn, global_conn


def test_variance_round_trip_against_right_run(tmp_path):
    conn, global_conn = _variance_client(tmp_path)

    # Run 1 anchored at 2026-01-30; its week 1 = 01-31 .. 02-06.
    run1 = compute_forecast(conn, "BASE")
    assert run1.anchor_date == "2026-01-30"
    assert run1.weeks[0].outflows["payroll"] == pytest.approx(1000.00, abs=0.01)
    save_forecast(conn, run1)

    # Stage 2: actuals land inside and beyond run 1's week-1 window.
    stage2 = [{"name": "Checking", "txns": [
        ("02/03/2026", "Deposit", "", "Customer", "Payment", "Checking",
         2500.00),
        ("02/06/2026", "Expense", "", "Gusto Payroll", "Payroll", "Checking",
         -1000.00),
        ("02/13/2026", "Expense", "", "Gusto Payroll", "Payroll", "Checking",
         -1000.00),
    ]}]
    import_gl(conn, parse_gl(write_gl(tmp_path / "v_gl2.csv", stage2,
              period="February, 2026-February, 2026")), "v_gl2.csv")

    # A second, later run whose week 1 has NOT elapsed.
    run2 = compute_forecast(conn, "BASE")
    assert run2.anchor_date == "2026-02-13"
    save_forecast(conn, run2)

    # Variance must score run 1 (elapsed week 1), not run 2.
    written = compute_variance(conn)
    assert len(written) == 2
    by_type = {r["row_type"]: r for r in written}
    assert by_type["inflow_total"]["forecast"] == pytest.approx(0.00, abs=0.01)
    assert by_type["inflow_total"]["actual"] == pytest.approx(2500.00, abs=0.01)
    assert by_type["outflow_total"]["forecast"] == pytest.approx(1000.00,
                                                                 abs=0.01)
    assert by_type["outflow_total"]["actual"] == pytest.approx(1000.00,
                                                               abs=0.01)
    assert all(r["week_ending"] == "2026-02-06" for r in written)

    logged = conn.execute(
        "SELECT run_id, row_type, forecast, actual FROM variance_log"
    ).fetchall()
    assert len(logged) == 2

    # Re-running finds no unscored, elapsed run.
    assert compute_variance(conn) == []

    conn.close()
    global_conn.close()
