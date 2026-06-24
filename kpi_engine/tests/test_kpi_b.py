"""KPI engine tests, CHUNK 6B: receivables + disbursements.

Two clients built through the REAL pipeline (COA + GL + pairings + aging
parsers, importers, classifier, dormancy pass), each constructed so every
asserted KPI is hand-checkable.

CLIENT 1 — AP-driven (has bills_payments). Exercises the amount-matcher on
both sides, the A/R aging snapshot (deliberately 40 days old → medium
confidence), and the A/P-only KPIs.

  A/R pairings (dso_true):
    Alpha   invoice 1000 @2025-01-01, payment 1000 @2025-01-31  → lag 30
    Beta    invoice  600 @2025-02-01, invoice 400 @2025-02-05,
            payment 1000 @2025-03-01 (2-invoice combo) → lags 28, 24
    DSO = (1000*30 + 600*28 + 400*24) / 2000 = 56400 / 2000 = 28.2 days

  A/P pairings (vendor_pay_lag):
    Welder   bill 500 @2025-01-01, payment 500 @2025-01-21        → lag 20
    Supply   bill 300 @2025-02-01, bill 200 @2025-02-03,
             payment 500 @2025-02-21 (combo)                      → 20, 18
    CreditCo bill 250 @2025-03-01, vendor credit 250 @2025-03-15  → lag 14
    lag = (500*20 + 300*20 + 200*18 + 250*14) / 1250 = 23100/1250 = 18.48

  A/R aging snapshot (as_of = today-40, invoice dates = today-40):
    91+:     Gamma 1500, Delta 1000   → ar_at_risk 2500, haircut 0.50
    61-90:   Echo  800                → haircut 0.75
    31-60:   Foxtrot 400              → haircut 1.00 (+2wk shift)
    CURRENT: Hotel 600                → haircut 1.00
    expected_collections = 1500*.5 + 1000*.5 + 800*.75 + 400 + 600 = 2850

  GL A/R: beginning 1000, +5000 billed, -3000 collected
    collection_effectiveness = 3000 / (1000 + 5000) = 0.5

  A/P aging snapshot: CURRENT Welder 500, 1-30 Supply 800 → open_ap_due 1300;
    nums carry no job prefixes → job_cash_demand omitted.

CLIENT 2 — direct-pay (cash only, no A/P ledger).
    Gusto Payroll: 6 weekly outflows averaging 1000 → weekly → 1000*4.33=4330
    City Properties rent: 4 monthly outflows of 2000 → monthly → 2000
    Equipment Depot: one 6000 outflow → NOT recurring
    recurring_outflow_base = 4330 + 2000 = 6330
    payroll_load = 6000 / (6000 + 8000 + 6000) = 0.30
    no bills/aging → archetype direct_pay, open_ap_due omitted.
"""

import datetime as dt

import csv

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
from core.kpi.disbursements import compute_disbursements
from core.kpi.receivables import compute_receivables
from core.parsers.aging import parse_aging
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from core.parsers.pairings import parse_pairings
from tests.test_importer import fmt, write_coa, write_gl

FOOTER = " Friday, June 12, 2026 02:45 AM GMTZ"


def _write(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


def write_ar_pairings(path, parties, period="January, 2025-June, 2026",
                      company="Test Co"):
    """parties: list of (name, [(date, txn_type, memo, num, amount)])."""
    rows = [
        [company],
        ["Invoices and Received Payments"],
        [period],
        [],
        ["", "Date", "Transaction type", "Memo/Description",
         "Transaction number", "Amount", "A/R paid", "Open Balance"],
    ]
    for name, items in parties:
        rows.append([name])
        for date, ttype, memo, num, amount in items:
            rows.append(["", date, ttype, memo, num, fmt(amount), "", "0.00"])
    rows.append([])
    rows.append([FOOTER])
    return _write(path, rows)


def write_ap_pairings(path, parties, period="January, 2025-June, 2026",
                      company="Test Co"):
    """parties: list of (vendor, [(date, txn_type, num, memo, amount)])."""
    rows = [
        [company],
        ["Bills and Applied Payments"],
        [period],
        [],
        ["", "Date", "Transaction type", "Transaction number",
         "Memo/Description", "Amount", "Open balance"],
    ]
    for name, items in parties:
        rows.append([name])
        for date, ttype, num, memo, amount in items:
            rows.append(["", date, ttype, num, memo, fmt(amount), "0.00"])
    rows.append([])
    rows.append([FOOTER])
    return _write(path, rows)


def write_ar_aging(path, as_of_text, buckets, company="Test Co"):
    """buckets: list of (name, [(date, ttype, num, customer, due, amount,
    open)])."""
    rows = [
        [company],
        ["A/R Aging Detail"],
        [as_of_text],
        [],
        ["", "Date", "Transaction type", "Num", "Customer full name",
         "Due date", "Amount", "Open balance"],
    ]
    grand = 0.0
    for name, items in buckets:
        rows.append([name])
        total = 0.0
        for date, ttype, num, party, due, amount, open_bal in items:
            rows.append(["", date, ttype, num, party, due, fmt(amount),
                         fmt(open_bal)])
            total += open_bal
        rows.append([f"Total for {name}", "", "", "", "", "", "", fmt(total)])
        grand += total
    rows.append(["TOTAL", "", "", "", "", "", "", fmt(grand)])
    rows.append([])
    rows.append([FOOTER])
    return _write(path, rows)


def write_ap_aging(path, as_of_text, buckets, company="Test Co"):
    """buckets: list of (name, [(date, ttype, num, vendor, due, past_due,
    amount, open)])."""
    rows = [
        [company],
        ["A/P Aging Detail"],
        [as_of_text],
        [],
        ["", "Date", "Transaction type", "Num", "Vendor display name",
         "Due date", "Past due", "Amount", "Open balance"],
    ]
    grand = 0.0
    for name, items in buckets:
        rows.append([name])
        total = 0.0
        for date, ttype, num, vendor, due, past_due, amount, open_bal in items:
            rows.append(["", date, ttype, num, vendor, due, str(past_due),
                         fmt(amount), fmt(open_bal)])
            total += open_bal
        rows.append([f"Total for {name}", "", "", "", "", "", "", "",
                     fmt(total)])
        grand += total
    rows.append(["TOTAL", "", "", "", "", "", "", "", fmt(grand)])
    rows.append([])
    rows.append([FOOTER])
    return _write(path, rows)


def by_key(kpis):
    return {k.key: k for k in kpis}


def _mdy(date: dt.date) -> str:
    return date.strftime("%m/%d/%Y")


# ── CLIENT 1: AP-driven ──────────────────────────────────────────────────────

AP_COA = [
    ("", "Checking", "Bank", "Checking", 9500.00),
    ("", "Accounts Receivable (A/R)", "Accounts receivable (A/R)",
     "Accounts Receivable", 3000.00),
    ("", "Visa Card", "Credit Card", "Credit Card", 0.00),
    ("", "Design Income", "Income", "Service/Fee Income", 0.00),
]

AP_GL = [
    {"name": "Checking", "beginning": 10000.00, "txns": [
        ("01/15/2025", "Expense", "", "Office Depot", "Supplies",
         "Office Supplies", -500.00),
    ]},
    {"name": "Accounts Receivable (A/R)", "beginning": 1000.00, "txns": [
        ("02/01/2025", "Invoice", "", "Customer", "Billing",
         "Design Income", 5000.00),
        ("03/01/2025", "Payment", "", "Customer", "Collection",
         "Checking", -3000.00),
    ]},
    {"name": "Design Income", "txns": [
        ("02/01/2025", "Invoice", "", "Customer", "Work",
         "Accounts Receivable (A/R)", -5000.00),
    ]},
]


@pytest.fixture
def ap_client(tmp_path):
    conn = db.get_client_db("apclient", base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    import_coa(conn, parse_coa(write_coa(tmp_path / "ap_coa.csv", AP_COA)))
    import_gl(conn, parse_gl(write_gl(tmp_path / "ap_gl.csv", AP_GL)),
              "ap_gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)

    import_pairings(conn, parse_pairings(write_ar_pairings(
        tmp_path / "ar_pair.csv",
        [
            ("Alpha", [
                ("01/31/2025", "Payment", "", "", 1000.00),
                ("01/01/2025", "Invoice", "Job", "A1", 1000.00),
            ]),
            ("Beta", [
                ("03/01/2025", "Payment", "", "", 1000.00),
                ("02/01/2025", "Invoice", "Part 1", "B1", 600.00),
                ("02/05/2025", "Invoice", "Part 2", "B2", 400.00),
            ]),
        ],
    )), "ar_pair.csv")

    import_pairings(conn, parse_pairings(write_ap_pairings(
        tmp_path / "ap_pair.csv",
        [
            ("Welder", [
                ("01/21/2025", "Bill Payment (Check)", "", "Pay", -500.00),
                ("01/01/2025", "Bill", "W1", "Weld", 500.00),
            ]),
            ("Supply", [
                ("02/21/2025", "Bill Payment (Check)", "", "Pay", -500.00),
                ("02/01/2025", "Bill", "S1", "Mat 1", 300.00),
                ("02/03/2025", "Bill", "S2", "Mat 2", 200.00),
            ]),
            ("CreditCo", [
                ("03/01/2025", "Bill", "C1", "Goods", 250.00),
                ("03/15/2025", "Vendor Credit", "VC1", "Return", -250.00),
            ]),
        ],
    )), "ap_pair.csv")

    snap_date = dt.date.today() - dt.timedelta(days=40)
    inv = _mdy(snap_date)
    due = _mdy(snap_date + dt.timedelta(days=30))
    import_aging(conn, parse_aging(write_ar_aging(
        tmp_path / "ar_aging.csv",
        snap_date.strftime("As of %b %d, %Y"),
        [
            ("91 or more days past due", [
                (inv, "Invoice", "G1", "Gamma", due, 1500.00, 1500.00),
                (inv, "Invoice", "G2", "Delta", due, 1000.00, 1000.00),
            ]),
            ("61 - 90 days past due", [
                (inv, "Invoice", "E1", "Echo", due, 800.00, 800.00),
            ]),
            ("31 - 60 days past due", [
                (inv, "Invoice", "F1", "Foxtrot", due, 400.00, 400.00),
            ]),
            ("CURRENT", [
                (inv, "Invoice", "H1", "Hotel", due, 600.00, 600.00),
            ]),
        ],
    )), "ar_aging.csv")

    import_aging(conn, parse_aging(write_ap_aging(
        tmp_path / "ap_aging.csv",
        "As of Jun 11, 2026",
        [
            ("CURRENT", [
                ("06/01/2026", "Bill", "500", "Welder", "06/20/2026", -19,
                 500.00, 500.00),
            ]),
            ("1 - 30 days past due", [
                ("05/15/2026", "Bill", "300", "Supply", "06/05/2026", 12,
                 800.00, 800.00),
            ]),
        ],
    )), "ap_aging.csv")

    yield conn, global_conn, tmp_path
    conn.close()
    global_conn.close()


def test_archetype_ap_driven(ap_client):
    conn, _, _ = ap_client
    kpis = compute_disbursements(conn)
    assert all(k.detail["archetype"] == "ap_driven" for k in kpis)


def test_dso_true_exact_and_combo(ap_client):
    conn, _, _ = ap_client
    dso = by_key(compute_receivables(conn))["dso_true"]
    assert dso.value == pytest.approx(28.2, abs=0.01)
    assert dso.detail["matched_count"] == 3
    assert dso.detail["unmatched_count"] == 0
    assert dso.detail["match_rate"] == pytest.approx(1.0)


def test_vendor_pay_lag_with_credit(ap_client):
    conn, _, _ = ap_client
    lag = by_key(compute_disbursements(conn))["vendor_pay_lag"]
    assert lag.value == pytest.approx(18.48, abs=0.01)
    assert lag.detail["by_vendor"]["Welder"] == pytest.approx(20.0)
    assert lag.detail["by_vendor"]["Supply"] == pytest.approx(19.0)
    assert lag.detail["by_vendor"]["CreditCo"] == pytest.approx(14.0)


def test_ar_at_risk(ap_client):
    conn, _, _ = ap_client
    at_risk = by_key(compute_receivables(conn))["ar_at_risk"]
    assert at_risk.value == pytest.approx(2500.00, abs=0.01)
    invoices = at_risk.detail["invoices"]
    assert [i["customer"] for i in invoices] == ["Gamma", "Delta"]
    assert invoices[0]["open_balance"] == pytest.approx(1500.00)


def test_expected_collections_haircuts(ap_client):
    conn, _, _ = ap_client
    expected = by_key(compute_receivables(conn))["expected_collections_13wk"]
    assert expected.value == pytest.approx(2850.00, abs=0.01)
    assert len(expected.detail["haircut_log"]) == 5
    assert len(expected.detail["weekly"]) == 13
    assert sum(expected.detail["weekly"]) == pytest.approx(2850.00, abs=0.01)


def test_overdue_collections_are_spread_not_piled_in_week_one(ap_client):
    conn, _, _ = ap_client
    expected = by_key(compute_receivables(conn))["expected_collections_13wk"]
    weekly = expected.detail["weekly"]
    # Every snapshot invoice here is overdue; recovery must be spread across
    # several weeks, not dumped into week 1.
    assert weekly[0] < expected.value  # week 1 is not the whole thing
    assert sum(1 for w in weekly if w > 0) >= 4  # collections land in many weeks


def test_collection_effectiveness(ap_client):
    conn, _, _ = ap_client
    eff = by_key(compute_receivables(conn))["collection_effectiveness"]
    assert eff.value == pytest.approx(0.5, abs=0.0001)
    assert eff.detail["collected"] == pytest.approx(3000.00)
    assert eff.detail["new_billings"] == pytest.approx(5000.00)
    assert eff.detail["beginning_ar"] == pytest.approx(1000.00)


def test_open_ap_due_present(ap_client):
    conn, _, _ = ap_client
    kpis = by_key(compute_disbursements(conn))
    open_ap = kpis["open_ap_due"]
    assert open_ap.value == pytest.approx(1300.00, abs=0.01)
    assert open_ap.detail["due_within_14"] == pytest.approx(1300.00, abs=0.01)
    assert open_ap.detail["due_within_30"] == pytest.approx(1300.00, abs=0.01)


def test_job_cash_demand_absent_without_prefixes(ap_client):
    conn, _, _ = ap_client
    assert "job_cash_demand" not in by_key(compute_disbursements(conn))


def test_ar_snapshot_staleness_lowers_confidence(ap_client):
    conn, _, _ = ap_client
    # Snapshot is 40 days old → medium across every receivables KPI.
    assert all(k.confidence == "medium" for k in compute_receivables(conn))


# ── CLIENT 2: direct-pay ─────────────────────────────────────────────────────

DIRECT_COA = [
    ("", "Checking", "Bank", "Checking", 5000.00),
]

# Six weekly payroll runs averaging exactly 1000, a monthly rent, a one-off.
DIRECT_GL = [
    {"name": "Checking", "beginning": 50000.00, "txns": [
        ("01/02/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Payroll", -980.00),
        ("01/09/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Payroll", -1020.00),
        ("01/16/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Payroll", -990.00),
        ("01/23/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Payroll", -1010.00),
        ("01/30/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Payroll", -1000.00),
        ("02/06/2026", "Expense", "", "Gusto Payroll", "Payroll",
         "Payroll", -1000.00),
        ("01/01/2026", "Expense", "", "City Properties", "Rent",
         "Rent", -2000.00),
        ("02/01/2026", "Expense", "", "City Properties", "Rent",
         "Rent", -2000.00),
        ("03/01/2026", "Expense", "", "City Properties", "Rent",
         "Rent", -2000.00),
        ("04/01/2026", "Expense", "", "City Properties", "Rent",
         "Rent", -2000.00),
        ("02/15/2026", "Check", "", "Equipment Depot", "New saw",
         "Equipment", -6000.00),
    ]},
]


@pytest.fixture
def direct_client(tmp_path):
    conn = db.get_client_db("directclient", base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    import_coa(conn, parse_coa(write_coa(tmp_path / "d_coa.csv", DIRECT_COA)))
    import_gl(conn, parse_gl(write_gl(tmp_path / "d_gl.csv", DIRECT_GL)),
              "d_gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    yield conn, global_conn, tmp_path
    conn.close()
    global_conn.close()


def test_archetype_direct_pay(direct_client):
    conn, _, _ = direct_client
    kpis = compute_disbursements(conn)
    assert all(k.detail["archetype"] == "direct_pay" for k in kpis)


def test_open_ap_due_absent_on_direct_pay(direct_client):
    conn, _, _ = direct_client
    assert "open_ap_due" not in by_key(compute_disbursements(conn))
    assert "vendor_pay_lag" not in by_key(compute_disbursements(conn))


def test_recurring_outflow_base(direct_client):
    conn, _, _ = direct_client
    recurring = by_key(compute_disbursements(conn))["recurring_outflow_base"]
    # Weekly payroll 1000*4.33 + monthly rent 2000*1 = 6330.
    assert recurring.value == pytest.approx(6330.00, abs=0.01)
    payees = {r["payee"]: r for r in recurring.detail["recurring"]}
    assert payees["Gusto Payroll"]["cadence"] == "weekly"
    assert payees["Gusto Payroll"]["n_seen"] == 6
    assert payees["Gusto Payroll"]["typical_amount"] == pytest.approx(1000.00)
    assert payees["City Properties"]["cadence"] == "monthly"
    # The single 6000 outflow is never flagged recurring.
    assert "Equipment Depot" not in payees


def test_payroll_load(direct_client):
    conn, _, _ = direct_client
    payroll = by_key(compute_disbursements(conn))["payroll_load"]
    # 6000 payroll of 20000 total outflow.
    assert payroll.value == pytest.approx(0.30, abs=0.0001)
    assert payroll.detail["match_basis"]["keyword"] == 6
