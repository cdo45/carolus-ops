"""CHUNK 9 tests: the localhost app server.

Each test spins a real server on a free port in a background thread and talks
to it over HTTP, then shuts it down.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.server import make_server, start_idle_watchdog

FIXTURES = Path(__file__).parent / "fixtures"


def _get(port, path):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=5
    ) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post_json(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _multipart(file_name, file_bytes, bucket=None):
    boundary = "----qbotestboundary"
    parts = []
    if bucket is not None:
        parts += [
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="bucket"',
            b"",
            bucket.encode(),
        ]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"'
        .encode(),
        b"Content-Type: text/csv",
        b"",
        file_bytes,
        f"--{boundary}--".encode(),
        b"",
    ]
    body = b"\r\n".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def _upload(port, slug, file_path, bucket=None, name=None):
    data = Path(file_path).read_bytes()
    body, ctype = _multipart(name or Path(file_path).name, data, bucket)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload?client={slug}",
        data=body, headers={"Content-Type": ctype}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _new_client(port, name="Acme Co"):
    _, body = _post_json(port, "/api/clients", {"name": name})
    return body["client"]["slug"]


@pytest.fixture
def server(tmp_path):
    srv = make_server(tmp_path / "appdata", port=None, idle_seconds=3600,
                      check_interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _, port = srv.server_address
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def test_health(server):
    _, port = server
    status, body = _get(port, "/api/health")
    assert status == 200
    assert body == {"ok": True, "status": "ok"}


def test_idle_timer_resets_on_request(server):
    srv, port = server
    before = srv.last_activity
    time.sleep(0.05)
    _get(port, "/api/health")
    assert srv.last_activity > before


def test_unknown_route_is_envelope_not_crash(server):
    _, port = server
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/nope")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        body = json.loads(exc.read().decode("utf-8"))
        assert body["ok"] is False
        assert "not found" in body["message"].lower()


def test_create_client_and_upload_gl(server):
    _, port = server
    slug = _new_client(port)
    status, body = _upload(
        port, slug, FIXTURES / "gl_with_beginning_balances.csv", bucket="GL"
    )
    assert status == 200
    assert body["ok"] is True
    assert body["report_type"] == "GL"
    assert body["misroute"] is False
    assert body["row_count"] > 0


def test_coa_into_gl_bucket_misroutes_but_imports(server):
    _, port = server
    slug = _new_client(port)
    _, body = _upload(
        port, slug, FIXTURES / "coa_full.csv", bucket="GL"
    )
    assert body["ok"] is True
    assert body["report_type"] == "COA"
    assert body["misroute"] is True
    assert "Chart of Accounts" in body["misroute_message"]
    # Imported as COA regardless of the bucket it was dropped in.
    _, status = _get(port, f"/api/status?client={slug}")
    coa = next(b for b in status["buckets"] if b["key"] == "COA")
    assert "account" in coa["loaded_label"]


def test_status_reflects_gl_coverage(server):
    _, port = server
    slug = _new_client(port)
    _upload(port, slug, FIXTURES / "gl_with_beginning_balances.csv", bucket="GL")
    _, status = _get(port, f"/api/status?client={slug}")
    gl = next(b for b in status["buckets"] if b["key"] == "GL")
    assert gl["loaded_label"].startswith("through ")


def test_run_returns_headline_and_dashboard(server):
    _, port = server
    slug = _new_client(port)
    _upload(port, slug, FIXTURES / "coa_full.csv", bucket="COA")
    _upload(port, slug, FIXTURES / "gl_with_beginning_balances.csv", bucket="GL")
    status, body = _post_json(port, f"/api/run?client={slug}", {})
    assert status == 200 and body["ok"] is True
    assert "cash" in body["headline"]
    assert "breach_weeks" in body["headline"]
    assert Path(body["dashboard_path"]).exists()


def test_dashboard_served_over_http(server):
    _, port = server
    slug = _new_client(port)
    _upload(port, slug, FIXTURES / "coa_full.csv", bucket="COA")
    _upload(port, slug, FIXTURES / "gl_with_beginning_balances.csv", bucket="GL")

    # Before a run there is no dashboard yet.
    before = urllib.request.Request(
        f"http://127.0.0.1:{port}/dashboard?client={slug}")
    try:
        urllib.request.urlopen(before, timeout=5)
        raise AssertionError("expected 404 before any run")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404

    _post_json(port, f"/api/run?client={slug}", {})
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/dashboard?client={slug}", timeout=10
    ) as resp:
        assert resp.status == 200
        assert resp.headers.get_content_type() == "text/html"
        body = resp.read().decode("utf-8")
    assert "<html" in body and 'id="data"' in body  # the real dashboard


def test_corrupted_gl_upload_returns_human_error_not_500(server):
    _, port = server
    slug = _new_client(port)
    broken = (
        "Test Co\n"
        "General Ledger\n"
        "January, 2026-January, 2026\n"
        ",\n"
        ",Transaction date,Transaction type,Num,Name,Description,Split,Amount,"
        "Balance\n"
        ",01/05/2026,Expense,,X,desc,Checking,-100.00,100.00\n"
    ).encode("utf-8")
    body, ctype = _multipart("broken_gl.csv", broken, "GL")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload?client={slug}",
        data=body, headers={"Content-Type": ctype}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        status = resp.status
        payload = json.loads(resp.read().decode("utf-8"))
    assert status == 200  # errors are data, not 500s
    assert payload["ok"] is False
    assert "account section" in payload["message"]


def test_idle_shutdown_fires(tmp_path):
    srv = make_server(tmp_path / "appdata", port=None, idle_seconds=0.2,
                      check_interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    start_idle_watchdog(srv)
    # The watchdog should stop serve_forever once idle exceeds 0.2s.
    thread.join(timeout=5)
    assert not thread.is_alive()
    srv.server_close()
