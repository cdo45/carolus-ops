"""Tests for GL-backfilled monthly KPI history."""

from core import db
from core.classify import classify_client
from core.importer import dormancy_pass, import_coa, import_gl
from core.kpi.history import monthly_history
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from tests.test_importer import write_coa, write_gl


def _client(tmp_path):
    conn = db.get_client_db("hist", base_dir=tmp_path / "app")
    global_conn = db.get_global_db(base_dir=tmp_path / "app")
    coa = [
        ("", "Checking", "Bank", "Checking", 0.0),
        ("", "Accounts Payable (A/P)", "Accounts payable (A/P)",
         "Accounts Payable", 0.0),
        ("", "Rent", "Expenses", "Rent or lease of buildings", 0.0),
    ]
    import_coa(conn, parse_coa(write_coa(tmp_path / "c.csv", coa)))
    gl = [
        {"name": "Checking", "beginning": 10000.0, "txns": [
            ("07/15/2025", "Expense", "", "L", "rent", "Rent", -1000.0),
            ("08/15/2025", "Expense", "", "L", "rent", "Rent", -1000.0),
            ("09/15/2025", "Expense", "", "L", "rent", "Rent", -1000.0),
        ]},
        {"name": "Accounts Payable (A/P)", "beginning": 2000.0, "txns": [
            ("07/10/2025", "Bill", "", "V", "m", "Rent", 500.0),
        ]},
    ]
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl.csv", gl,
              period="July, 2025-September, 2025")), "gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    global_conn.close()
    return conn


def test_monthly_history_reconstructs_balances_from_gl(tmp_path):
    conn = _client(tmp_path)
    hist = monthly_history(conn)
    conn.close()

    # One point per month across the GL period; the last point clamps to the
    # final transaction date (Sep 15), since there's no data after it.
    cash = hist["cash_on_hand"]
    assert [p["date"] for p in cash] == ["2025-07-31", "2025-08-31",
                                         "2025-09-15"]
    # Cash declines $1,000 a month from the $10,000 opening.
    assert [round(p["value"]) for p in cash] == [9000, 8000, 7000]
    # Current ratio is computed where there are current liabilities.
    assert len(hist["current_ratio"]) == 3
    # July: assets 9000 / liabilities (A/P 2500) = 3.6.
    assert round(hist["current_ratio"][0]["value"], 2) == 3.6


def test_rolling_pl_metrics_backfilled_from_gl(tmp_path):
    conn = db.get_client_db("roll", base_dir=tmp_path / "app")
    global_conn = db.get_global_db(base_dir=tmp_path / "app")
    coa = [
        ("", "Checking", "Bank", "Checking", 0.0),
        ("", "Design Income", "Income", "Service/Fee Income", 0.0),
        ("", "Direct Labor", "Cost of Goods Sold", "Cost of labor - COS", 0.0),
        ("", "Rent", "Expenses", "Rent or lease of buildings", 0.0),
    ]
    import_coa(conn, parse_coa(write_coa(tmp_path / "c.csv", coa)))
    months = ["07", "08", "09", "10", "11", "12"]
    gl = [
        {"name": "Design Income", "txns": [
            (f"{mm}/01/2025", "Invoice", "", "C", "j", "Checking", -10000.0)
            for mm in months]},
        {"name": "Direct Labor", "txns": [
            (f"{mm}/05/2025", "Check", "", "Crew", "w", "Checking", 4000.0)
            for mm in months]},
        {"name": "Rent", "txns": [
            (f"{mm}/05/2025", "Expense", "", "L", "r", "Checking", 2000.0)
            for mm in months]},
    ]
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl.csv", gl,
              period="July, 2025-December, 2025")), "gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    hist = monthly_history(conn)
    conn.close()
    global_conn.close()

    # Trailing-quarter window: $30k revenue, $12k direct labor, $6k rent.
    gm = hist["gross_margin"]
    assert gm  # emitted once a full 3-month window exists
    assert round(gm[-1]["value"], 2) == 0.60  # (30000 - 12000) / 30000
    assert round(hist["direct_labor_pct"][-1]["value"], 1) == 40.0
    assert round(hist["overhead_burn"][-1]["value"]) == 2000  # 6000 / 3
    assert round(hist["breakeven_revenue"][-1]["value"], 2) == 3333.33


def test_monthly_history_empty_without_period(tmp_path):
    conn = db.get_client_db("empty", base_dir=tmp_path / "app")
    try:
        assert monthly_history(conn) == {}
    finally:
        conn.close()
