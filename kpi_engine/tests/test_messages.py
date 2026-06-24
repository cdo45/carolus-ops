"""CHUNK 12 message-contract audit.

Every user-reachable error/notice that flows back through the API must be a
human message: no Python class names, no "Traceback", no file paths. Each
documented trigger must also carry its contracted phrase.
"""

import datetime as dt
import json
import threading
import urllib.request

import pytest

from app.server import make_server
from tests.test_kpi_b import write_ar_aging
from tests.test_importer import write_gl

# Substrings that would betray a leaked technical message.
FORBIDDEN = ["Traceback", "Error", "Exception", ".py", "/home/", "C:\\",
             "sqlite3", "NoneType"]


def _hygienic(message: str) -> None:
    assert message, "message must not be empty"
    for bad in FORBIDDEN:
        assert bad not in message, f"message leaked {bad!r}: {message!r}"


def _post_json(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_raw(port, path, raw_bytes):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=raw_bytes,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upload(port, slug, filename, data: bytes, bucket=None):
    boundary = "----qbomsg"
    parts = []
    if bucket is not None:
        parts += [f"--{boundary}".encode(),
                  b'Content-Disposition: form-data; name="bucket"', b"",
                  bucket.encode()]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'
        .encode(),
        b"Content-Type: text/csv", b"", data,
        f"--{boundary}--".encode(), b"",
    ]
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload?client={slug}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def srv(tmp_path):
    server = make_server(tmp_path / "appdata", port=None, idle_seconds=3600,
                         check_interval=0.05)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, port = server.server_address
    slug = _post_json(port, "/api/clients", {"name": "Msg Co"})["client"]["slug"]
    yield port, slug, tmp_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


CORRUPTED_GL = (
    "Test Co\n"
    "General Ledger\n"
    '"January, 2026-January, 2026"\n'
    ",\n"
    ",Transaction date,Transaction type,Num,Name,Description,Split,Amount,"
    "Balance\n"
    "Checking\n"
    ",notadate,Expense,,Vendor,Memo,Cash,-100.00,100.00\n"
).encode("utf-8")

# Files used for the wrong-bucket cases.
COA_BYTES = (
    "Test Co\nChart of Accounts\nAs of May 31, 2026\n,\n"
    "Account #,Full name,Type,Detail type,Balance\n"
    ",Checking,Bank,Checking,1000.00\n"
).encode("utf-8")
GL_BYTES = (
    "Test Co\nGeneral Ledger\n\"January, 2026-January, 2026\"\n,\n"
    ",Transaction date,Transaction type,Num,Name,Description,Split,Amount,"
    "Balance\n"
    "Checking\n,Beginning Balance,,,,,,,1000.00\n"
    ",01/05/2026,Expense,,V,m,Office,-100.00,900.00\n"
    "Total for Checking,,,,,,,$-100.00,\n"
).encode("utf-8")


def test_wrong_file_gl_bucket_misroute_message(srv):
    port, slug, _ = srv
    res = _upload(port, slug, "accounts.csv", COA_BYTES, bucket="GL")
    assert res["ok"] is True and res["misroute"] is True
    _hygienic(res["misroute_message"])
    assert "right spot" in res["misroute_message"]


def test_wrong_file_coa_bucket_misroute_message(srv):
    port, slug, _ = srv
    res = _upload(port, slug, "ledger.csv", GL_BYTES, bucket="COA")
    assert res["ok"] is True and res["misroute"] is True
    _hygienic(res["misroute_message"])
    assert "right spot" in res["misroute_message"]


def test_corrupted_gl_row_human_error(srv):
    port, slug, _ = srv
    res = _upload(port, slug, "broken.csv", CORRUPTED_GL, bucket="GL")
    assert res["ok"] is False
    _hygienic(res["message"])
    assert "General Ledger" in res["message"]


def test_stale_aging_warning(srv):
    port, slug, tmp_path = srv
    old = (dt.date.today() - dt.timedelta(days=45))
    path = write_ar_aging(
        tmp_path / "ar.csv", old.strftime("As of %b %d, %Y"),
        [("CURRENT", [(old.strftime("%m/%d/%Y"), "Invoice", "1", "Cust",
                       old.strftime("%m/%d/%Y"), 100.0, 100.0)])],
    )
    res = _upload(port, slug, "ar.csv", path.read_bytes(), bucket="AR_AGING")
    assert res["ok"] is True
    stale = [w for w in res["warnings"] if "days old" in w]
    assert stale, res["warnings"]
    _hygienic(stale[0])


def test_period_shortening_notice(srv):
    port, slug, tmp_path = srv
    wide = write_gl(tmp_path / "wide.csv", [
        {"name": "Checking", "beginning": 1000.00, "txns": [
            ("01/05/2026", "Expense", "", "V", "m", "Office", -100.00),
            ("12/05/2026", "Expense", "", "V", "m", "Office", -100.00),
        ]}], period="January, 2026-December, 2026")
    _upload(port, slug, "wide.csv", wide.read_bytes(), bucket="GL")
    narrow = write_gl(tmp_path / "narrow.csv", [
        {"name": "Checking", "beginning": 1000.00, "txns": [
            ("01/10/2026", "Expense", "", "V", "m", "Office", -50.00),
        ]}], period="January, 2026-January, 2026")
    res = _upload(port, slug, "narrow.csv", narrow.read_bytes(), bucket="GL")
    assert res["ok"] is True
    retained = [w for w in res["warnings"] if "retained" in w]
    assert retained, res["warnings"]
    _hygienic(retained[0])


def test_unknown_exception_fallback(srv):
    port, slug, _ = srv
    # scenario must be a mapping; a list triggers an unexpected error path.
    res = _post_json(port, f"/api/config?client={slug}", {"scenario": [1, 2]})
    assert res["ok"] is False
    _hygienic(res["message"])
    assert "unexpected" in res["message"].lower()


def test_malformed_json_body_does_not_leak(srv):
    port, slug, _ = srv
    res = _post_raw(port, "/api/clients", b"{not valid json")
    assert res["ok"] is False
    _hygienic(res["message"])
