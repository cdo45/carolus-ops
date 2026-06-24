"""CHUNK 12 end-to-end smoke test — the release gate.

Exercises the whole product through the public API the way the wizard does:
create a client, upload the COA + GL fixtures, confirm the mapping, set a cash
floor, run, and check every deliverable (dashboard, embedded data, COA
proposal), an identical re-upload diffing to zero, and a populated audit feed.
"""

import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from app.server import make_server

FIXTURES = Path(__file__).parent / "fixtures"
DATA_RE = re.compile(
    r'<script type="application/json" id="data">(.*?)</script>', re.S
)


def _get(port, path):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=20
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upload(port, slug, file_path, bucket):
    boundary = "----qboe2e"
    data = Path(file_path).read_bytes()
    body = b"\r\n".join([
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="bucket"', b"", bucket.encode(),
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{Path(file_path).name}"'.encode(),
        b"Content-Type: text/csv", b"", data,
        f"--{boundary}--".encode(), b"",
    ])
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload?client={slug}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def server(tmp_path):
    srv = make_server(tmp_path / "appdata", port=None, idle_seconds=3600,
                      check_interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _, port = srv.server_address
    yield port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def test_full_release_flow(server):
    port = server

    # 1) Health.
    assert _get(port, "/api/health")["ok"] is True

    # 2) Create client (wizard step 1).
    slug = _post(port, "/api/clients", {"name": "Release Co"})["client"]["slug"]

    # 3) Upload COA + GL (wizard step 2).
    coa = _upload(port, slug, FIXTURES / "coa_full.csv", "COA")
    gl = _upload(port, slug, FIXTURES / "gl_with_beginning_balances.csv", "GL")
    assert coa["ok"] and coa["report_type"] == "COA"
    assert gl["ok"] and gl["report_type"] == "GL"

    # 4) Confirm the mapping (wizard step 3).
    mapping = _get(port, f"/api/mapping?client={slug}")
    assert mapping["ok"] and "queue" in mapping["counts"]
    confirmed = _post(port, f"/api/mapping?client={slug}", {"confirm_all": True})
    assert confirmed["ok"] is True

    # 5) Set the cash floor (wizard step 4).
    assert _post(port, f"/api/config?client={slug}",
                 {"cash_floor": 1000})["ok"] is True

    # 6) Run (wizard step 5) → dashboard exists with valid embedded JSON.
    run = _post(port, f"/api/run?client={slug}", {})
    assert run["ok"] is True
    dash = Path(run["dashboard_path"])
    assert dash.exists()
    data = json.loads(DATA_RE.search(dash.read_text(encoding="utf-8")).group(1))
    assert set(data["forecast"]) == {"BASE", "STRETCH", "CRUNCH"}
    assert len(data["forecast"]["BASE"]["weeks"]) == 13
    assert "cash" in run["headline"]

    # 7) COA standardization proposal generates both files.
    proposal = _post(port, f"/api/proposal?client={slug}", {})
    assert proposal["ok"] is True
    assert Path(proposal["html_path"]).exists()
    assert Path(proposal["csv_path"]).exists()

    # 8) An identical GL re-upload diffs to zero.
    again = _upload(port, slug, FIXTURES / "gl_with_beginning_balances.csv", "GL")
    assert again["ok"] is True
    assert again["diff"] == {"changed": 0, "added": 0, "removed": 0}

    # 9) The audit feed is populated.
    audit = _get(port, f"/api/audit?client={slug}")
    assert audit["ok"] is True
    assert audit["items"]
