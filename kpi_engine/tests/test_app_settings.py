"""CHUNK 11 tests: settings config round-trip and the humanized audit feed."""

import json
import threading
import urllib.request

import pytest

from app.server import make_server
from core import db
from core.classify import classify_client
from core.importer import dormancy_pass, import_coa
from core.parsers.coa import parse_coa
from tests.test_importer import write_coa


def _post(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(port, path):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=15
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def _new_client(base_dir, tmp_path, slug="acme", name="Acme"):
    conn = db.get_client_db(slug, base_dir=base_dir)
    global_conn = db.get_global_db(base_dir=base_dir)
    coa = [
        ("", "Checking", "Bank", "Checking", 1000.00),
        ("", "Mystery Holding", "Other Assets", "Holding", 500.00),
    ]
    import_coa(conn, parse_coa(write_coa(tmp_path / "coa.csv", coa)))
    classify_client(conn, global_conn)
    dormancy_pass(conn)
    conn.close()
    global_conn.close()
    registry = db.load_registry(base_dir=base_dir)
    registry.setdefault("clients", []).append(
        {"slug": slug, "name": name, "last_dashboard": None}
    )
    db.save_registry(registry, base_dir=base_dir)
    return slug


def test_config_round_trip(srv):
    _, port, base_dir, tmp_path = srv
    slug = _new_client(base_dir, tmp_path)
    res = _post(port, f"/api/config?client={slug}", {
        "cash_floor": 7500,
        "archetype": "direct_pay",
        "scenario": {"CRUNCH": {"haircut_91": 0.1}},
        "manual_billing_schedule": '[{"week": 3, "amount": 2000}]',
    })
    assert res["ok"] is True
    cfg = _get(port, f"/api/config?client={slug}")["config"]
    assert cfg["cash_floor"] == "7500.0"
    assert cfg["archetype"] == "direct_pay"
    assert cfg["scenario"]["CRUNCH"]["haircut_91"] == "0.1"
    assert json.loads(cfg["manual_billing_schedule"]) == \
        [{"week": 3, "amount": 2000.0}]


def test_invalid_manual_schedule_rejected_with_human_message(srv):
    _, port, base_dir, tmp_path = srv
    slug = _new_client(base_dir, tmp_path)
    bad_json = _post(port, f"/api/config?client={slug}",
                     {"manual_billing_schedule": "not json at all"})
    assert bad_json["ok"] is False
    assert "valid JSON" in bad_json["message"]
    bad_week = _post(port, f"/api/config?client={slug}",
                     {"manual_billing_schedule": '[{"week": 20, "amount": 1}]'})
    assert bad_week["ok"] is False
    assert "between 1 and 13" in bad_week["message"]


def test_archetype_override_drives_status_greying(srv):
    _, port, base_dir, tmp_path = srv
    slug = _new_client(base_dir, tmp_path)
    _post(port, f"/api/config?client={slug}", {"archetype": "ap_driven"})
    status = _get(port, f"/api/status?client={slug}")
    assert status["archetype"] == "ap_driven"
    ap = next(b for b in status["buckets"] if b["key"] == "AP_AGING")
    assert ap["greyed"] is False  # AP no longer greyed once forced AP-driven


def test_audit_feed_humanizes_category_change(srv):
    _, port, base_dir, tmp_path = srv
    slug = _new_client(base_dir, tmp_path)
    conn = db.get_client_db(slug, base_dir=base_dir)
    aid = conn.execute(
        "SELECT id FROM accounts WHERE qbo_name = 'Mystery Holding'"
    ).fetchone()["id"]
    conn.close()
    # A user mapping change writes a humanizable category audit row.
    _post(port, f"/api/mapping?client={slug}",
          {"changes": [{"id": aid, "category": "OCA"}]})
    feed = _get(port, f"/api/audit?client={slug}")
    assert feed["ok"] is True
    assert feed["items"]
    top = feed["items"][0]["text"]
    assert "Mystery Holding" in top
    assert "moved from" in top and "to" in top
    assert "you" in top  # source 'user' humanized
