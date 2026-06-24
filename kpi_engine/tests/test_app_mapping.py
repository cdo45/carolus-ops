"""CHUNK 10 tests: the mapping API + wizard config, end to end on a server."""

import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from app.server import make_server
from core import db
from core.classify import classify_client
from core.importer import dormancy_pass, import_coa, import_gl
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl
from tests.test_importer import write_coa, write_gl

DATA_RE = re.compile(
    r'<script type="application/json" id="data">(.*?)</script>', re.S
)


def _get(port, path):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=10
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gl_sections():
    return [
        {"name": "Checking", "beginning": 5000.00, "txns": [
            ("01/05/2026", "Expense", "", "Gusto Payroll", "Pay",
             "Office Payroll", -1000.00),
            ("01/12/2026", "Expense", "", "Gusto Payroll", "Pay",
             "Office Payroll", -1000.00),
            ("01/19/2026", "Expense", "", "Gusto Payroll", "Pay",
             "Office Payroll", -1000.00),
        ]},
        {"name": "Design Income", "txns": [
            ("01/05/2026", "Invoice", "", "Cust", "Job", "Checking", -2000.00),
        ]},
    ]


def _make_client(base_dir, tmp_path, slug, name, oddball):
    conn = db.get_client_db(slug, base_dir=base_dir)
    global_conn = db.get_global_db(base_dir=base_dir)
    coa = [
        ("", "Checking", "Bank", "Checking", 5000.00),
        ("", "Design Income", "Income", "Service/Fee Income", 0.00),
        ("", oddball, "Other Assets", "Holding", 500.00),
    ]
    import_coa(conn, parse_coa(write_coa(tmp_path / (slug + "_coa.csv"), coa)))
    import_gl(conn, parse_gl(write_gl(tmp_path / (slug + "_gl.csv"),
              _gl_sections(), period="January, 2026-January, 2026")), "gl.csv")
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    conn.close()
    global_conn.close()
    registry = db.load_registry(base_dir=base_dir)
    registry.setdefault("clients", []).append(
        {"slug": slug, "name": name, "last_dashboard": None}
    )
    db.save_registry(registry, base_dir=base_dir)


@pytest.fixture
def srv(tmp_path):
    base_dir = tmp_path / "appdata"
    server = make_server(base_dir, port=None, idle_seconds=3600,
                         check_interval=0.05)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, port = server.server_address
    yield server, port, base_dir, tmp_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _account_id(base_dir, slug, name):
    conn = db.get_client_db(slug, base_dir=base_dir)
    try:
        return conn.execute(
            "SELECT id FROM accounts WHERE qbo_name = ?", (name,)
        ).fetchone()["id"]
    finally:
        conn.close()


def _account_row(base_dir, slug, name):
    conn = db.get_client_db(slug, base_dir=base_dir)
    try:
        return conn.execute(
            "SELECT category, confidence, status FROM accounts "
            "WHERE qbo_name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()


def test_mapping_get_groups_by_tier(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "alpha", "Alpha", "Mystery Holding")
    m = _get(port, "/api/mapping?client=alpha")
    assert m["ok"] is True
    queue_names = [r["qbo_name"] for r in m["groups"]["queue"]]
    auto_names = [r["qbo_name"] for r in m["groups"]["auto"]]
    assert "Mystery Holding" in queue_names
    assert "Checking" in auto_names and "Design Income" in auto_names
    row = m["groups"]["auto"][0]
    assert {"id", "label", "explanation", "dormant", "category"} <= set(row)
    # The CASH row carries its plain-English label from the taxonomy.
    cash = next(r for r in m["groups"]["auto"] if r["qbo_name"] == "Checking")
    assert cash["category"] == "CASH" and cash["label"]


def test_post_change_confirms_and_audits(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "beta", "Beta", "Mystery Holding")
    aid = _account_id(base_dir, "beta", "Mystery Holding")
    res = _post(port, "/api/mapping?client=beta",
                {"changes": [{"id": aid, "category": "OCA"}]})
    assert res["ok"] is True and res["confirmed"] == 1
    row = _account_row(base_dir, "beta", "Mystery Holding")
    assert row["category"] == "OCA"
    assert row["confidence"] == 100
    assert row["status"] == "confirmed"
    conn = db.get_client_db("beta", base_dir=base_dir)
    try:
        audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity_id = ? AND "
            "field = 'category' AND source = 'user'", (aid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert audit == 1


def test_confirm_all_flips_only_proposed_non_queue(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "gamma", "Gamma", "Mystery Holding")
    res = _post(port, "/api/mapping?client=gamma", {"confirm_all": True})
    assert res["ok"] is True
    assert res["confirmed_all"] == 2  # Checking + Design Income
    assert _account_row(base_dir, "gamma", "Checking")["status"] == "confirmed"
    # The queue account (NULL category) is left for the human.
    assert _account_row(base_dir, "gamma", "Mystery Holding")["status"] == \
        "proposed"


def test_unsure_status_set_and_dashboard_still_generates(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "delta", "Delta", "Mystery Holding")
    aid = _account_id(base_dir, "delta", "Mystery Holding")
    _post(port, "/api/mapping?client=delta", {"unsure": [aid]})
    assert _account_row(base_dir, "delta", "Mystery Holding")["status"] == \
        "unsure"
    run = _post(port, "/api/run?client=delta", {})
    assert run["ok"] is True
    html = Path(run["dashboard_path"]).read_text(encoding="utf-8")
    data = json.loads(DATA_RE.search(html).group(1))
    # The unmapped account surfaces in the bookkeeping review queue.
    assert "Mystery Holding" in data["flags"]["queue"]


def test_remember_alias_used_by_second_client(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "epsilon", "Epsilon", "Zorptastic Holdings")
    aid = _account_id(base_dir, "epsilon", "Zorptastic Holdings")
    res = _post(port, "/api/mapping?client=epsilon", {
        "changes": [{"id": aid, "category": "OCA"}],
        "remember": [{"id": aid, "category": "OCA"}],
    })
    assert res["remembered"] == 1

    # A brand-new client B with the same oddball leaf name inherits the alias.
    conn = db.get_client_db("zeta", base_dir=base_dir)
    global_conn = db.get_global_db(base_dir=base_dir)
    coa = [("", "Zorptastic Holdings", "Other Assets", "Holding", 250.00)]
    import_coa(conn, parse_coa(write_coa(tmp_path / "zeta_coa.csv", coa)))
    classify_client(conn, global_conn)
    row = conn.execute(
        "SELECT category, confidence FROM accounts WHERE qbo_name = ?",
        ("Zorptastic Holdings",),
    ).fetchone()
    conn.close()
    global_conn.close()
    assert row["category"] == "OCA"
    assert row["confidence"] == 95  # learned-alias confidence


def test_confirmed_account_survives_reclassification(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "eta", "Eta", "Mystery Holding")
    aid = _account_id(base_dir, "eta", "Mystery Holding")
    _post(port, "/api/mapping?client=eta",
          {"changes": [{"id": aid, "category": "OCA"}]})
    # A subsequent GET re-runs the classifier; the confirmed row is untouched.
    _get(port, "/api/mapping?client=eta")
    row = _account_row(base_dir, "eta", "Mystery Holding")
    assert row["category"] == "OCA" and row["status"] == "confirmed"


def test_deleted_accounts_are_parked_not_queued(srv):
    _, port, base_dir, tmp_path = srv
    conn = db.get_client_db("iota", base_dir=base_dir)
    global_conn = db.get_global_db(base_dir=base_dir)
    coa = [
        ("", "Checking", "Bank", "Checking", 1000.00),
        ("", "Old Truck (deleted)", "Fixed Assets", "Vehicles", 5000.00),
        ("", "Mystery Holding", "Other Assets", "Holding", 500.00),
    ]
    import_coa(conn, parse_coa(write_coa(tmp_path / "iota.csv", coa)))
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    conn.close()
    global_conn.close()
    registry = db.load_registry(base_dir=base_dir)
    registry.setdefault("clients", []).append(
        {"slug": "iota", "name": "Iota", "last_dashboard": None}
    )
    db.save_registry(registry, base_dir=base_dir)

    m = _get(port, "/api/mapping?client=iota")
    queue_names = [r["qbo_name"] for r in m["groups"]["queue"]]
    ignored_names = [r["qbo_name"] for r in m["groups"]["ignored"]]
    # The deleted account is parked, the live unmapped one still needs input.
    assert any("Old Truck" in n for n in ignored_names)
    assert not any("Old Truck" in n for n in queue_names)
    assert "Mystery Holding" in queue_names
    # And the home review-queue count ignores deleted accounts.
    status = _get(port, "/api/status?client=iota")
    assert status["queue_count"] == 1  # only Mystery Holding


def test_auto_classified_account_can_be_recategorized(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "lambda", "Lambda", "Mystery Holding")
    # Checking auto-classifies as CASH (confidence 95); a human can still move
    # it. The mapping screen now exposes this on the auto group too.
    m = _get(port, "/api/mapping?client=lambda")
    cash = next(r for r in m["groups"]["auto"] if r["qbo_name"] == "Checking")
    assert cash["category"] == "CASH" and cash["confidence"] >= 90
    res = _post(port, "/api/mapping?client=lambda",
                {"changes": [{"id": cash["id"], "category": "OCA"}]})
    assert res["ok"] is True and res["confirmed"] == 1
    row = _account_row(base_dir, "lambda", "Checking")
    assert row["category"] == "OCA" and row["status"] == "confirmed"


def test_explicit_ignore_action_parks_a_live_account(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "kappa", "Kappa", "Mystery Holding")
    aid = _account_id(base_dir, "kappa", "Mystery Holding")
    res = _post(port, "/api/mapping?client=kappa", {"ignore": [aid]})
    assert res["ok"] is True and res["ignored"] == 1
    m = _get(port, "/api/mapping?client=kappa")
    ignored_names = [r["qbo_name"] for r in m["groups"]["ignored"]]
    assert "Mystery Holding" in ignored_names
    assert _get(port, "/api/status?client=kappa")["queue_count"] == 0


def test_wizard_cash_floor_lands_in_config_and_drives_breach(srv):
    _, port, base_dir, tmp_path = srv
    _make_client(base_dir, tmp_path, "theta", "Theta", "Mystery Holding")
    cfg = _post(port, "/api/config?client=theta", {"cash_floor": 999999999})
    assert cfg["ok"] is True and cfg["config"]["cash_floor"] == "999999999.0"
    conn = db.get_client_db("theta", base_dir=base_dir)
    try:
        stored = conn.execute(
            "SELECT value FROM config WHERE key = 'cash_floor'"
        ).fetchone()["value"]
    finally:
        conn.close()
    assert stored == "999999999.0"
    run = _post(port, "/api/run?client=theta", {})
    assert run["ok"] is True
    # An impossibly high floor forces every forecast week to breach.
    assert run["headline"]["breach_weeks"]
