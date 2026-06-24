"""KPI engine tests, CHUNK 6A: liquidity + revenue.

One client built through the REAL pipeline (COA + GL parsers, importers,
classifier, dormancy pass), constructed so every KPI is hand-checkable.

Hand-check sheet (period 2025-06-15 .. 2026-05-31, as_of = 2026-05-31):
  Checking   = 10000 +5000 -1000 -2600 -2600 -1300 +2000 = 9500
  Savings    = 5000 + 500 = 5500          → cash_on_hand 15000
  13-week outflow window 2026-03-02..05-31: 2600+2600+1300 = 6500
    → avg weekly 500 → weeks_of_cash 30.0
  AR = 2000 +3000 -1000 = 4000;  AP = 1500 +2500 -1000 = 3000
  CC = 800 + 700 = 1500;  TAXL = 500
    → NWC (15000+4000) - (3000+1500+500) = 14000;  ratio 19000/5000 = 3.8
  card_debt_load = 1500 / (30000/3) = 0.15
  quick_burn = (15000+4000) - (3000+1500+500*4) = 12500
  Revenue: Design -8000 x 12 = 96000; Construction -1000 x 3, -2000 x 3
    = 9000 → revenue_t12 105000, all negative → convention "negative"
  Full months Jul'25..May'26; last 3 avg 10000 vs prior 3 avg 9000
    → trend +11.11%
  Direct: DL 21000 + SUB 10500 + DMAT 10500 = 42000
    → gross_margin 0.6;  direct_labor_pct (21000+10500)/105000 = 30.0%
  Overhead: OH 3000 + OH-PAY 15000 + OH-INS 6000 + OH-OCC 12000 = 36000
    over 12 months → burn 3000 (DEP 5000 excluded)
    → breakeven 3000 / 0.6 = 5000
  "Old Money Market": GL beginning 24797.35, zero txns, COA balance 0
    → dormant + zero → EXCLUDED (cash would be 39797.35 if included)
  13 "Reserve Account" banks (COA balance 1.00, no GL) stay counted, so
    one unmapped BS account later = 1/20 = 0.05 → confidence "medium".
"""

import pytest

from core import db
from core.classify import classify_client
from core.importer import dormancy_pass, import_coa, import_gl
from core.kpi.liquidity import compute_liquidity
from core.kpi.revenue import compute_revenue
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from tests.test_importer import write_coa, write_gl

COA_ACCOUNTS = [
    # (number, full_path, qbo_type, detail_type, balance)
    ("", "Checking", "Bank", "Checking", 9500.00),
    ("", "Savings", "Bank", "Savings", 5500.00),
    ("", "Old Money Market", "Bank", "Money Market", 0.00),
    ("", "Accounts Receivable (A/R)", "Accounts receivable (A/R)",
     "Accounts Receivable", 4000.00),
    ("", "Accounts Payable (A/P)", "Accounts payable (A/P)",
     "Accounts Payable", 3000.00),
    ("", "Visa Card", "Credit Card", "Credit Card", 1500.00),
    ("", "Sales Tax Payable", "Other Current Liabilities",
     "Sales Tax Payable", 500.00),
    ("", "Design Income", "Income", "Service/Fee Income", 0.00),
    ("", "Construction Income", "Income", "Service/Fee Income", 0.00),
    ("", "Direct Labor", "Cost of Goods Sold", "Cost of labor - COS", 0.00),
    ("", "Subcontractors", "Cost of Goods Sold", "Cost of labor - COS", 0.00),
    ("", "Job Materials", "Cost of Goods Sold",
     "Supplies & materials - COGS", 0.00),
    ("", "Rent", "Expenses", "Rent or lease of buildings", 0.00),
    ("", "Office Supplies", "Expenses",
     "Office/general administrative expenses", 0.00),
    ("", "Insurance", "Expenses", "Insurance", 0.00),
    ("", "Office Payroll", "Expenses", "Payroll expenses", 0.00),
    ("", "Depreciation Expense", "Expenses", "Depreciation", 0.00),
] + [
    # Filler banks: dormant after the pass but coa_balance 1.00 keeps them
    # counted, giving the BS side 19 mapped accounts.
    ("", f"Reserve Account {i}", "Bank", "Checking", 1.00) for i in range(13)
]

DESIGN_MONTHS = [
    "06/20/2025", "07/20/2025", "08/20/2025", "09/20/2025", "10/20/2025",
    "11/20/2025", "12/20/2025", "01/20/2026", "02/20/2026", "03/20/2026",
    "04/20/2026", "05/20/2026",
]

GL_SECTIONS = [
    {"name": "Checking", "beginning": 10000.00, "txns": [
        ("06/15/2025", "Deposit", "", "Customer A", "Invoice payment",
         "Accounts Receivable (A/R)", 5000.00),
        ("07/10/2025", "Expense", "", "Vendor X", "Supplies",
         "Office Supplies", -1000.00),
        ("03/15/2026", "Expense", "", "Landlord", "Rent", "Rent", -2600.00),
        ("04/15/2026", "Expense", "", "Landlord", "Rent", "Rent", -2600.00),
        ("05/15/2026", "Expense", "", "Landlord", "Rent", "Rent", -1300.00),
        ("05/31/2026", "Deposit", "", "Customer B", "Invoice payment",
         "Accounts Receivable (A/R)", 2000.00),
    ]},
    {"name": "Savings", "beginning": 5000.00, "txns": [
        ("08/01/2025", "Transfer", "", "", "From checking", "Checking",
         500.00),
    ]},
    # Dormant-exclusion bait: a real beginning balance, zero transactions,
    # and a zero COA balance. If the exclusion rule fails, cash jumps.
    {"name": "Old Money Market", "beginning": 24797.35, "txns": []},
    {"name": "Accounts Receivable (A/R)", "beginning": 2000.00, "txns": [
        ("09/01/2025", "Invoice", "2001", "Customer A", "Project",
         "Design Income", 3000.00),
        ("02/01/2026", "Payment", "", "Customer A", "Payment", "Checking",
         -1000.00),
    ]},
    {"name": "Accounts Payable (A/P)", "beginning": 1500.00, "txns": [
        ("10/01/2025", "Bill", "", "Vendor Z", "Materials", "Job Materials",
         2500.00),
        ("01/15/2026", "Bill Payment", "", "Vendor Z", "", "Checking",
         -1000.00),
    ]},
    {"name": "Visa Card", "beginning": 800.00, "txns": [
        ("11/05/2025", "Expense", "", "Gas Station", "Fuel", "Checking",
         700.00),
    ]},
    {"name": "Sales Tax Payable", "txns": [
        ("03/20/2026", "Journal Entry", "", "", "Sales tax accrual", "",
         500.00),
    ]},
    {"name": "Design Income", "txns": [
        (date, "Invoice", "", "Various", "Design work",
         "Accounts Receivable (A/R)", -8000.00)
        for date in DESIGN_MONTHS
    ]},
    {"name": "Construction Income", "txns": [
        ("12/10/2025", "Invoice", "", "Builder Co", "Phase work",
         "Accounts Receivable (A/R)", -1000.00),
        ("01/10/2026", "Invoice", "", "Builder Co", "Phase work",
         "Accounts Receivable (A/R)", -1000.00),
        ("02/10/2026", "Invoice", "", "Builder Co", "Phase work",
         "Accounts Receivable (A/R)", -1000.00),
        ("03/10/2026", "Invoice", "", "Builder Co", "Phase work",
         "Accounts Receivable (A/R)", -2000.00),
        ("04/10/2026", "Invoice", "", "Builder Co", "Phase work",
         "Accounts Receivable (A/R)", -2000.00),
        ("05/10/2026", "Invoice", "", "Builder Co", "Phase work",
         "Accounts Receivable (A/R)", -2000.00),
    ]},
    {"name": "Direct Labor", "txns": [
        ("07/05/2025", "Check", "", "Crew", "Field wages",
         "Accounts Payable (A/P)", 20000.00),
        ("04/10/2026", "Check", "", "Crew", "Field wages",
         "Accounts Payable (A/P)", 1000.00),
    ]},
    {"name": "Subcontractors", "txns": [
        ("08/15/2025", "Bill", "", "Sub LLC", "Tile work",
         "Accounts Payable (A/P)", 10500.00),
    ]},
    {"name": "Job Materials", "txns": [
        ("10/01/2025", "Bill", "", "Vendor Z", "Materials",
         "Accounts Payable (A/P)", 10500.00),
    ]},
    {"name": "Rent", "txns": [
        ("07/01/2025", "Expense", "", "Landlord", "Shop rent", "Checking",
         6000.00),
        ("01/02/2026", "Expense", "", "Landlord", "Shop rent", "Checking",
         6000.00),
    ]},
    {"name": "Office Supplies", "txns": [
        ("09/05/2025", "Expense", "", "Vendor X", "Supplies", "Checking",
         3000.00),
    ]},
    {"name": "Insurance", "txns": [
        ("06/30/2025", "Expense", "", "Premier Insurance", "Premium",
         "Checking", 6000.00),
    ]},
    {"name": "Office Payroll", "txns": [
        ("11/15/2025", "Check", "", "Admin", "Office wages", "Checking",
         7500.00),
        ("02/15/2026", "Check", "", "Admin", "Office wages", "Checking",
         7500.00),
    ]},
    {"name": "Depreciation Expense", "txns": [
        ("12/31/2025", "Journal Entry", "", "", "Annual depreciation",
         "Accumulated Depreciation", 5000.00),
    ]},
]


@pytest.fixture
def client(tmp_path):
    conn = db.get_client_db("kpitest", base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    import_coa(conn, parse_coa(write_coa(tmp_path / "coa.csv", COA_ACCOUNTS)))
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl.csv", GL_SECTIONS)),
              "gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    yield conn, global_conn, tmp_path
    conn.close()
    global_conn.close()


def by_key(kpis):
    return {k.key: k for k in kpis}


def add_unmapped_bs_account(conn, global_conn, tmp_path):
    """An 'Other Assets' account no waterfall stage can place: BS-side
    (type class) but unmapped (category NULL). Non-zero balance so it is
    never excluded."""
    path = write_coa(tmp_path / "coa_mystery.csv", [
        ("", "Mystery Holding", "Other Assets", "Unknown Detail", 123.00),
    ])
    import_coa(conn, parse_coa(path))
    classify_client(conn, global_conn)


# ── liquidity ────────────────────────────────────────────────────────────────

def test_cash_on_hand_and_dormant_exclusion(client):
    conn, _, _ = client
    # The bait account really is dormant with a zero COA balance...
    row = conn.execute(
        "SELECT dormant, coa_balance FROM accounts "
        "WHERE qbo_name = 'Old Money Market'"
    ).fetchone()
    assert row["dormant"] == 1
    assert row["coa_balance"] == 0
    # ...and its 24797.35 beginning balance stays out of cash.
    cash = by_key(compute_liquidity(conn))["cash_on_hand"]
    assert cash.value == pytest.approx(15000.00, abs=0.01)
    assert cash.detail["Checking"] == pytest.approx(9500.00, abs=0.01)
    assert cash.detail["Savings"] == pytest.approx(5500.00, abs=0.01)
    assert "Old Money Market" not in cash.detail
    assert cash.unit == "currency"


def test_weeks_of_cash(client):
    conn, _, _ = client
    weeks = by_key(compute_liquidity(conn))["weeks_of_cash"]
    assert weeks.value == pytest.approx(30.0, abs=0.01)
    assert weeks.detail["avg_weekly_outflow"] == pytest.approx(500.00,
                                                               abs=0.01)
    assert weeks.detail["window_start"] == "2026-03-02"
    assert weeks.detail["window_end"] == "2026-05-31"


def test_net_working_capital_and_current_ratio(client):
    conn, _, _ = client
    kpis = by_key(compute_liquidity(conn))
    nwc = kpis["net_working_capital"]
    assert nwc.value == pytest.approx(14000.00, abs=0.01)
    assert nwc.detail["assets"]["CASH"] == pytest.approx(15000.00, abs=0.01)
    assert nwc.detail["assets"]["AR"] == pytest.approx(4000.00, abs=0.01)
    assert nwc.detail["liabilities"]["AP"] == pytest.approx(3000.00, abs=0.01)
    assert nwc.detail["liabilities"]["CC"] == pytest.approx(1500.00, abs=0.01)
    assert nwc.detail["liabilities"]["TAXL"] == pytest.approx(500.00,
                                                              abs=0.01)
    assert kpis["current_ratio"].value == pytest.approx(3.8, abs=0.01)


def test_card_debt_load_and_quick_burn(client):
    conn, _, _ = client
    kpis = by_key(compute_liquidity(conn))
    card = kpis["card_debt_load"]
    assert card.value == pytest.approx(0.15, abs=0.01)
    assert card.unit == "months"
    assert card.detail["avg_monthly_revenue"] == pytest.approx(10000.00,
                                                               abs=0.01)
    burn_check = kpis["quick_burn_check"]
    assert burn_check.value == pytest.approx(12500.00, abs=0.01)
    assert burn_check.detail["four_weeks_outflow"] == pytest.approx(
        2000.00, abs=0.01)


def test_liquidity_confidence_drops_with_unmapped_bs_account(client):
    conn, global_conn, tmp_path = client
    assert all(k.confidence == "high" for k in compute_liquidity(conn))
    add_unmapped_bs_account(conn, global_conn, tmp_path)
    # 1 unmapped of 20 counted BS accounts = 0.05 → medium, every KPI.
    after = compute_liquidity(conn)
    assert all(k.confidence == "medium" for k in after)
    # Revenue confidence keys off the P&L side — untouched.
    assert all(k.confidence == "high" for k in compute_revenue(conn))


# ── revenue ─────────────────────────────────────────────────────────────────

def test_revenue_t12_and_sign_convention(client):
    conn, _, _ = client
    t12 = by_key(compute_revenue(conn))["revenue_t12"]
    assert t12.value == pytest.approx(105000.00, abs=0.01)
    assert t12.detail["sign_convention"] == "negative"
    monthly = t12.detail["monthly"]
    assert len(monthly) == 12
    assert monthly["2025-06"] == pytest.approx(8000.00, abs=0.01)
    assert monthly["2025-12"] == pytest.approx(9000.00, abs=0.01)
    assert monthly["2026-05"] == pytest.approx(10000.00, abs=0.01)


def test_revenue_trend_and_mix(client):
    conn, _, _ = client
    kpis = by_key(compute_revenue(conn))
    trend = kpis["revenue_trend_3mo"]
    # Last 3 full months avg 10000 vs prior 3 avg 9000 → +11.11%.
    assert trend.value == pytest.approx(11.11, abs=0.01)
    assert trend.detail["last_3_months"] == ["2026-03", "2026-04", "2026-05"]
    assert trend.detail["prior_3_months"] == ["2025-12", "2026-01", "2026-02"]
    mix = kpis["revenue_mix"]
    assert mix.value is None
    assert mix.detail["Design Income"] == pytest.approx(91.43, abs=0.01)
    assert mix.detail["Construction Income"] == pytest.approx(8.57, abs=0.01)


def test_gross_margin_and_direct_labor_pct(client):
    conn, _, _ = client
    kpis = by_key(compute_revenue(conn))
    margin = kpis["gross_margin"]
    assert margin.value == pytest.approx(0.60, abs=0.01)
    assert margin.detail["direct_costs"] == pytest.approx(42000.00, abs=0.01)
    labor = kpis["direct_labor_pct"]
    assert labor.value == pytest.approx(30.0, abs=0.01)
    assert labor.detail["direct_labor"] == pytest.approx(31500.00, abs=0.01)


def test_overhead_burn_excludes_dep_and_breakeven(client):
    conn, _, _ = client
    kpis = by_key(compute_revenue(conn))
    burn = kpis["overhead_burn"]
    # 36000 over 12 months; with the 5000 DEP it would read 3416.67.
    assert burn.value == pytest.approx(3000.00, abs=0.01)
    assert "DEP" not in burn.detail["by_category"]
    assert "TAXE" not in burn.detail["by_category"]
    assert burn.detail["by_category"]["OH-OCC"] == pytest.approx(12000.00,
                                                                 abs=0.01)
    assert kpis["breakeven_revenue"].value == pytest.approx(5000.00,
                                                            abs=0.01)


def test_revenue_per_job_omitted_then_present(client):
    conn, _, tmp_path = client
    assert "revenue_per_job" not in by_key(compute_revenue(conn))
    # A later GL upload (June 2026, outside the existing range) brings
    # job-prefixed invoice numbers.
    job_gl = write_gl(tmp_path / "gl_jobs.csv", [
        {"name": "Design Income", "txns": [
            ("06/15/2026", "Invoice", "55-1001", "Customer J", "Job work",
             "Accounts Receivable (A/R)", -2500.00),
            ("06/20/2026", "Invoice", "55-1002", "Customer J", "Job work",
             "Accounts Receivable (A/R)", -1500.00),
        ]},
    ], period="June, 2026-June, 2026")
    import_gl(conn, parse_gl(job_gl), "gl_jobs.csv")
    per_job = by_key(compute_revenue(conn))["revenue_per_job"]
    assert per_job.value is None
    assert per_job.detail == {"55": pytest.approx(4000.00, abs=0.01)}
