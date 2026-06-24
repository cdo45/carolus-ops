"""CHUNK 8 golden tests: the offline HTML dashboard.

Builds the 6B AP-driven client, generates the dashboard, and asserts the hard
offline guarantees on the rendered HTML string.
"""

import datetime as dt
import json
import re

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
from core.parsers.aging import parse_aging
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from core.parsers.pairings import parse_pairings
from dashboard.generate import generate_dashboard
from tests.test_importer import write_coa, write_gl
from tests.test_kpi_b import (
    AP_COA,
    AP_GL,
    write_ap_aging,
    write_ar_aging,
    write_ar_pairings,
)

DATA_RE = re.compile(
    r'<script type="application/json" id="data">(.*?)</script>', re.S
)


def _build_ap_client(tmp_path):
    conn = db.get_client_db("apclient", base_dir=tmp_path / "appdata")
    global_conn = db.get_global_db(base_dir=tmp_path / "appdata")
    import_coa(conn, parse_coa(write_coa(tmp_path / "coa.csv", AP_COA)))
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl.csv", AP_GL)), "gl.csv")
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

    snap_date = dt.date.today() - dt.timedelta(days=40)
    inv = snap_date.strftime("%m/%d/%Y")
    due = (snap_date + dt.timedelta(days=30)).strftime("%m/%d/%Y")
    import_aging(conn, parse_aging(write_ar_aging(
        tmp_path / "ar_aging.csv", snap_date.strftime("As of %b %d, %Y"),
        [("91 or more days past due", [
            (inv, "Invoice", "G1", "Gamma", due, 1500.00, 1500.00),
        ]),
         ("CURRENT", [
            (inv, "Invoice", "H1", "Hotel", due, 600.00, 600.00),
        ])],
    )), "ar_aging.csv")

    import_aging(conn, parse_aging(write_ap_aging(
        tmp_path / "ap_aging.csv", "As of Jun 11, 2026",
        [("CURRENT", [
            ("06/01/2026", "Bill", "500", "Welder", "06/20/2026", -19,
             500.00, 500.00),
        ])],
    )), "ap_aging.csv")

    # A URL that will surface in embedded data (a CASH account name feeds the
    # cash-on-hand drill-down) plus a memo URL — both must be scrubbed.
    conn.execute(
        "UPDATE accounts SET qbo_name = 'Checking https://pay.example.com/x' "
        "WHERE qbo_name = 'Checking'"
    )
    conn.execute(
        "UPDATE transactions SET description = 'invoice at http://vendor.example'"
        " WHERE id = (SELECT id FROM transactions LIMIT 1)"
    )
    conn.commit()
    return conn, global_conn


@pytest.fixture
def generated(tmp_path):
    conn, global_conn = _build_ap_client(tmp_path)
    # Force a floor breach so the breach styling has something to mark.
    conn.execute("INSERT INTO config (key, value) VALUES ('cash_floor', ?)",
                 ("999999999",))
    conn.commit()
    path = generate_dashboard(conn, "Acme Services Inc", tmp_path / "out")
    html_text = path.read_text(encoding="utf-8")
    data = json.loads(DATA_RE.search(html_text).group(1))
    yield path, html_text, data, conn, global_conn, tmp_path
    conn.close()
    global_conn.close()


def test_single_html_root(generated):
    _, html_text, _, _, _, _ = generated
    assert html_text.count("<html") == 1
    assert html_text.count("</html>") == 1


def test_no_external_urls_anywhere(generated):
    _, html_text, _, _, _, _ = generated
    assert "http://" not in html_text
    assert "https://" not in html_text


def test_embedded_json_has_three_scenarios_and_13_weeks(generated):
    _, _, data, _, _, _ = generated
    assert set(data["forecast"]) == {"BASE", "STRETCH", "CRUNCH"}
    for scn in ("BASE", "STRETCH", "CRUNCH"):
        assert len(data["forecast"][scn]["weeks"]) == 13


def test_breach_styling_present_when_floor_breached(generated):
    _, html_text, data, _, _, _ = generated
    assert data["forecast"]["BASE"]["breach_weeks"]  # non-empty
    assert "breach" in html_text


def test_none_kpi_renders_dash(generated):
    _, html_text, data, _, _, _ = generated
    # aging_distribution carries a None value on this client.
    receivables = {k["key"]: k for k in data["kpis"]["receivables"]}
    assert receivables["aging_distribution"]["value"] is None
    assert "—" in html_text


def test_print_stylesheet_present(generated):
    _, html_text, _, _, _, _ = generated
    assert "@media print" in html_text
    assert "page-break-before" in html_text


def test_file_under_2mb(generated):
    path, _, _, _, _, _ = generated
    assert path.stat().st_size < 2_000_000


def test_presentation_is_human_readable(generated):
    _, html_text, _, _, _, _ = generated
    # An executive summary a finance person can read aloud.
    assert "Executive summary" in html_text
    # Plain-English "why it matters" on the KPIs.
    assert "Why it matters" in html_text
    # KPI proof is rendered as tables, not raw JSON dumps. The only <pre>
    # blocks are the appendix data backstop.
    assert "table class='detail'" in html_text
    # A real, labelled cash chart (not the old two-line stub).
    assert "cashChart" in html_text and "Floor" in html_text
    # Confidence reads as plain language, never a scary red "LOW".
    assert ">LOW<" not in html_text


def test_detail_only_tiles_get_a_headline_and_chart(generated):
    _, html_text, data, _, _, _ = generated
    by_key = {k["key"]: k for sec in data["kpis"].values() for k in sec}
    lag = by_key["customer_payment_lag"]
    aging = by_key["aging_distribution"]
    # Detail-only KPIs (value None) still surface a headline number.
    assert lag["value"] is None and lag["headline"]
    assert aging["value"] is None and "current" in aging["headline"].lower()
    # And each carries a chart spec the modal can draw.
    assert lag["chart"] and lag["chart"]["type"] == "bar"
    assert aging["chart"] and aging["chart"]["values"]
    # The modal chart renderer and KPI lookup are present.
    assert "function kpiChart" in html_text and "KPIMAP" in html_text


def test_change_banner_appears_after_user_edit(generated):
    path, html_text, _, conn, _, tmp_path = generated
    # Baseline: no user edits → no change banner.
    assert "bookkeeping change" not in html_text

    acct_id = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO audit_log (ts, entity, entity_id, field, old_value, "
        "new_value, source) VALUES (?, 'accounts', ?, 'category', '{}', "
        "'{}', 'user')",
        (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         acct_id),
    )
    conn.commit()

    regenerated = generate_dashboard(
        conn, "Acme Services Inc", tmp_path / "out2"
    ).read_text(encoding="utf-8")
    assert "bookkeeping change" in regenerated
    assert "category updated" in regenerated
