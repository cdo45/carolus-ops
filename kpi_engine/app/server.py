"""Localhost app server (stdlib http.server only).

A ThreadingHTTPServer bound to 127.0.0.1 on the first free port in 8400-8499.
Pages are served from app/static/ (read at request time, inlined — no external
requests); JSON lives under /api/*. All state is the on-disk registry plus the
per-client SQLite databases; nothing client-specific is held in module globals
beyond a single request.

Every exception path returns the error envelope {"ok": false, "message": ...}
with a human, message-contract string (what happened -> what to do) and HTTP
200 — parse failures are data, not server errors. Unexpected exceptions are
logged in full to <appdata>/logs/app.log and reported with a generic line.

The server shuts itself down after 30 minutes of inactivity; every request
resets the timer and a daemon watchdog enforces it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import db
from core.classify import DATA_DIR, classify_client, tier_for
from core.detect import TB_UNSUPPORTED_MESSAGE, detect_report_type
from core.importer import (
    import_aging,
    import_coa,
    import_gl,
    import_pairings,
)
from core.kpi.base import NOT_EXCLUDED
from core.kpi.disbursements import compute_disbursements
from core.kpi.forecast import (
    SCENARIO_KEYS,
    compute_forecast,
    compute_variance,
    save_forecast,
)
from core.kpi.liquidity import compute_liquidity
from core.kpi.receivables import compute_receivables
from core.kpi.revenue import compute_revenue
from core.parsers.aging import AgingParseError, parse_aging
from core.parsers.coa import COAParseError, parse_coa
from core.parsers.gl import GLParseError, parse_gl
from core.parsers.pairings import PairingsParseError, parse_pairings
from dashboard.generate import _change_banner, generate_dashboard

PORT_LOW = 8400
PORT_HIGH = 8499
IDLE_SECONDS = 30 * 60
STATIC_DIR = db.resource_path("app", "static")

UNEXPECTED_MESSAGE = (
    "Something unexpected went wrong reading this file. Try re-exporting it "
    "from QBO."
)

_PARSE_ERRORS = (GLParseError, COAParseError, AgingParseError, PairingsParseError)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}

# GET path -> static page basename (served with its CSS/JS inlined).
_PAGES = {"/": "index", "/mapping": "mapping", "/wizard": "wizard",
          "/settings": "settings"}
# page -> (css files, js files) inlined into <!--STYLE--> / <!--SCRIPT-->.
_PAGE_ASSETS = {
    "index": (["app.css"], ["app.js"]),
    "mapping": (["mapping.css"], ["mapping.js"]),
    "wizard": (["mapping.css", "wizard.css"], ["mapping.js", "wizard.js"]),
    "settings": (["settings.css"], ["settings.js"]),
}

# (method, path) -> handler(handler_instance, query_dict). Filled in below and
# extended by the API module section.
ROUTES: dict[tuple[str, str], object] = {}


def route(method: str, path: str):
    def register(fn):
        ROUTES[(method, path)] = fn
        return fn

    return register


# ── server + idle watchdog ───────────────────────────────────────────────────

class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def touch(self) -> None:
        with self._idle_lock:
            self.last_activity = time.monotonic()


def find_free_port(low: int = PORT_LOW, high: int = PORT_HIGH,
                   host: str = "127.0.0.1") -> int:
    for port in range(low, high + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port available in {low}-{high}.")


def make_server(
    base_dir: str | Path,
    host: str = "127.0.0.1",
    port: int | None = None,
    idle_seconds: float = IDLE_SECONDS,
    check_interval: float = 5.0,
) -> AppServer:
    if port is None:
        port = find_free_port(host=host)
    server = AppServer((host, port), Handler)
    server.base_dir = Path(base_dir)
    server.static_dir = STATIC_DIR
    server.idle_seconds = idle_seconds
    server.check_interval = check_interval
    server._idle_lock = threading.Lock()
    server.last_activity = time.monotonic()
    return server


def start_idle_watchdog(server: AppServer) -> threading.Thread:
    def loop():
        while True:
            time.sleep(server.check_interval)
            with server._idle_lock:
                idle = time.monotonic() - server.last_activity
            if idle >= server.idle_seconds:
                server.shutdown()
                return

    thread = threading.Thread(target=loop, name="idle-watchdog", daemon=True)
    thread.start()
    return thread


# ── request handler ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "QBOKpiApp/1.0"

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass

    # -- dispatch ----------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self.server.touch()
        parsed = urlparse(self.path)
        path = parsed.path
        if len(path) > 1:
            path = path.rstrip("/")
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if method == "GET" and path in _PAGES:
                return self._serve_page(_PAGES[path])
            if method == "GET" and path.startswith("/static/"):
                return self._serve_static(path)
            handler = ROUTES.get((method, path))
            if handler is None:
                return self.send_json(
                    {"ok": False, "message": "Page not found."}, status=404
                )
            handler(self, query)
        except _PARSE_ERRORS as exc:
            self.send_json({"ok": False, "message": str(exc)})
        except json.JSONDecodeError:
            # A malformed request body — never leak the parser's raw message.
            self.send_json({
                "ok": False,
                "message": "We couldn't read that request. Please try the "
                           "action again.",
            })
        except ValueError as exc:
            # The codebase raises ValueError with human-facing messages.
            self.send_json({"ok": False, "message": str(exc)})
        except Exception:
            self._log_traceback()
            self.send_json({"ok": False, "message": UNEXPECTED_MESSAGE})

    # -- helpers -----------------------------------------------------------
    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _serve_page(self, name: str) -> None:
        index = self.server.static_dir / f"{name}.html"
        if not index.exists():
            return self.send_bytes(
                b"<!DOCTYPE html><html><body><p>App static files not "
                b"installed yet.</p></body></html>",
                _CONTENT_TYPES[".html"],
            )
        page = index.read_text(encoding="utf-8")
        css_files, js_files = _PAGE_ASSETS.get(
            name, ([f"{name}.css"], [f"{name}.js"])
        )

        def inline(files):
            parts = []
            for fname in files:
                asset = self.server.static_dir / fname
                if asset.exists():
                    parts.append(asset.read_text(encoding="utf-8"))
            return "\n".join(parts)

        page = page.replace("<!--STYLE-->", f"<style>{inline(css_files)}</style>")
        page = page.replace("<!--SCRIPT-->", f"<script>{inline(js_files)}</script>")
        self.send_bytes(page.encode("utf-8"), _CONTENT_TYPES[".html"])

    def _serve_static(self, path: str) -> None:
        rel = path[len("/static/"):]
        target = (self.server.static_dir / rel).resolve()
        if (
            self.server.static_dir.resolve() not in target.parents
            or not target.exists()
        ):
            return self.send_json(
                {"ok": False, "message": "Asset not found."}, status=404
            )
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self.send_bytes(target.read_bytes(), ctype)

    def _log_traceback(self) -> None:
        try:
            log_dir = self.server.base_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "app.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{dt.datetime.now().isoformat()}] "
                        f"{self.command} {self.path}\n")
                f.write(traceback.format_exc())
        except Exception:
            pass


# ── routes ───────────────────────────────────────────────────────────────────

@route("GET", "/api/health")
def _api_health(handler: Handler, query: dict) -> None:
    handler.send_json({"ok": True, "status": "ok"})


# ── report families + helpers ────────────────────────────────────────────────

_FRIENDLY = {
    "COA": "Chart of Accounts",
    "GL": "General Ledger",
    "AR_AGING": "A/R Aging Detail",
    "AP_AGING": "A/P Aging Detail",
    "INVOICES_PAYMENTS": "Invoices & Received Payments",
    "BILLS_PAYMENTS": "Bills & Applied Payments",
}
STALE_AGING_DAYS = 30
_QUEUE_SQL = (
    "SELECT COUNT(*) FROM accounts a WHERE status != 'confirmed' "
    "AND a.inactive_candidate = 0 "
    "AND (a.category IS NULL OR a.confidence < 70) AND "
    "NOT (a.dormant = 1 AND (a.coa_balance IS NULL OR a.coa_balance = 0))"
)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "upload.csv"


def _client_dir(server: AppServer, slug: str) -> Path:
    return db.client_dir(slug, base_dir=server.base_dir)


def _registry_entry(server: AppServer, slug: str) -> dict | None:
    registry = db.load_registry(base_dir=server.base_dir)
    for entry in registry.get("clients", []):
        if entry.get("slug") == slug:
            return entry
    return None


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    """Minimal multipart/form-data reader: field name -> (filename, bytes)."""
    marker = content_type.find("boundary=")
    if marker == -1:
        return {}
    boundary = content_type[marker + len("boundary="):].strip().strip('"')
    delim = b"--" + boundary.encode()
    fields: dict[str, tuple[str | None, bytes]] = {}
    for raw in body.split(delim):
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        if not raw or raw.startswith(b"--"):
            continue
        if b"\r\n\r\n" not in raw:
            continue
        head, data = raw.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head_text = head.decode("utf-8", "replace")
        disp = next(
            (l for l in head_text.split("\r\n")
             if l.lower().startswith("content-disposition")),
            "",
        )
        name = re.search(r'name="([^"]*)"', disp)
        if not name:
            continue
        filename = re.search(r'filename="([^"]*)"', disp)
        fields[name.group(1)] = (
            filename.group(1) if filename else None, data
        )
    return fields


def _detect_and_import(conn, path: Path, bucket: str | None) -> dict:
    """Detect a saved export by content, route to the right importer, and
    return a UI summary. `bucket` is the UI's guess; mismatch sets misroute."""
    detection = detect_report_type(path)
    rtype = detection.report_type
    filename = path.name

    if rtype == "TB":
        raise ValueError(TB_UNSUPPORTED_MESSAGE)
    if rtype == "UNKNOWN":
        raise ValueError(
            "This file doesn't look like a QBO report we recognize (Chart of "
            "Accounts, General Ledger, Aging Detail, or Invoices/Bills). "
            "Re-export it from QBO."
        )

    warnings: list[str] = []
    diff: dict = {}
    if rtype == "COA":
        result = parse_coa(path)
        report = import_coa(conn, result)
        row_count = len(result.accounts)
        diff = {"new": len(report.new_accounts), "updated": report.updated_count}
    elif rtype == "GL":
        result = parse_gl(path)
        report = import_gl(conn, result, filename)
        row_count = result.row_count
        warnings += report.warnings + report.notes
        diff = {
            "changed": report.diff.changed_count,
            "added": report.diff.added_count,
            "removed": report.diff.removed_count,
        }
    elif rtype in ("AR_AGING", "AP_AGING"):
        result = parse_aging(path)
        report = import_aging(conn, result, filename)
        row_count = result.row_count
        warnings += result.warnings
        diff = {
            "inserted": report.inserted_count,
            "replaced": report.replaced,
        }
    else:  # INVOICES_PAYMENTS / BILLS_PAYMENTS
        result = parse_pairings(path)
        report = import_pairings(conn, result, filename)
        row_count = result.row_count
        warnings += report.warnings + report.notes
        diff = {
            "changed": report.diff.changed_count,
            "added": report.diff.added_count,
            "removed": report.diff.removed_count,
        }

    as_of = detection.as_of_date
    if rtype in ("AR_AGING", "AP_AGING") and as_of:
        age = (dt.date.today() - dt.date.fromisoformat(as_of)).days
        if age > STALE_AGING_DAYS:
            warnings.append(
                f"This {_FRIENDLY[rtype]} is {age} days old (as of {as_of}). "
                "Upload a fresher export for accurate aging."
            )

    bucket_family = (bucket or "").upper() or None
    misroute = bucket_family is not None and bucket_family != rtype
    misroute_message = (
        f"This looks like a {_FRIENDLY[rtype]} — it's been placed in the "
        "right spot."
        if misroute
        else None
    )

    period = None
    if detection.period_start or detection.period_end:
        period = {"start": detection.period_start, "end": detection.period_end}

    return {
        "report_type": rtype,
        "friendly": _FRIENDLY[rtype],
        "period": period,
        "as_of": as_of,
        "row_count": row_count,
        "diff": diff,
        "warnings": warnings,
        "misroute": misroute,
        "misroute_message": misroute_message,
    }


# ── client + upload + status + run routes ────────────────────────────────────

@route("GET", "/api/clients")
def _api_clients_list(handler: Handler, query: dict) -> None:
    registry = db.load_registry(base_dir=handler.server.base_dir)
    handler.send_json({"ok": True, "clients": registry.get("clients", [])})


@route("POST", "/api/clients")
def _api_clients_create(handler: Handler, query: dict) -> None:
    payload = json.loads(handler.read_body() or b"{}")
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("Enter a name for the new client.")
    server = handler.server
    registry = db.load_registry(base_dir=server.base_dir)
    existing = {c["slug"] for c in registry.get("clients", [])}
    base_slug = _slugify(name) or "client"
    slug = base_slug
    n = 2
    while slug in existing:
        slug = f"{base_slug}-{n}"
        n += 1
    db.get_client_db(slug, base_dir=server.base_dir).close()
    entry = {
        "slug": slug,
        "name": name,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "last_dashboard": None,
    }
    registry.setdefault("clients", []).append(entry)
    db.save_registry(registry, base_dir=server.base_dir)
    handler.send_json({"ok": True, "client": entry})


@route("POST", "/api/upload")
def _api_upload(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    if not slug or _registry_entry(server, slug) is None:
        raise ValueError("Pick a client before uploading a file.")
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        raise ValueError("Upload the report as a file, not as text.")
    fields = parse_multipart(ctype, handler.read_body())
    file_field = next(
        ((fn, data) for fn, data in fields.values() if fn is not None), None
    )
    if file_field is None or not file_field[1]:
        raise ValueError("No file was attached to the upload.")
    filename, data = file_field
    bucket_field = fields.get("bucket")
    bucket = (
        bucket_field[1].decode("utf-8", "replace")
        if bucket_field
        else query.get("bucket")
    )

    exports = _client_dir(server, slug) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    saved = exports / _safe_name(filename or "upload.csv")
    saved.write_bytes(data)

    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        summary = _detect_and_import(conn, saved, bucket)
    finally:
        conn.close()

    processed = exports / "processed"
    processed.mkdir(exist_ok=True)
    shutil.move(str(saved), str(processed / saved.name))

    summary["ok"] = True
    handler.send_json(summary)


def _build_status(conn) -> dict:
    def scalar(sql):
        return conn.execute(sql).fetchone()[0]

    bills_rows = scalar("SELECT COUNT(*) FROM bills_payments")
    ap_snap = conn.execute(
        "SELECT id, as_of_date FROM ap_aging_snapshots "
        "ORDER BY as_of_date DESC, id DESC LIMIT 1"
    ).fetchone()
    ap_nonempty = ap_snap is not None and conn.execute(
        "SELECT 1 FROM ap_aging_rows WHERE snapshot_id = ? LIMIT 1",
        (ap_snap["id"],),
    ).fetchone() is not None
    archetype = "ap_driven" if (bills_rows or ap_nonempty) else "direct_pay"
    override = conn.execute(
        "SELECT value FROM config WHERE key = 'archetype'"
    ).fetchone()
    if override and override["value"] in ("ap_driven", "direct_pay"):
        archetype = override["value"]
    greyed = archetype == "direct_pay"

    def aging(table):
        row = conn.execute(
            f"SELECT MAX(as_of_date) AS d FROM {table}"
        ).fetchone()
        as_of = row["d"]
        stale = None
        if as_of:
            stale = (dt.date.today() - dt.date.fromisoformat(as_of)).days
        return as_of, stale

    ar_as_of, ar_stale = aging("ar_aging_snapshots")
    ap_as_of, ap_stale = aging("ap_aging_snapshots")
    gl_end = scalar("SELECT MAX(period_end) FROM uploads WHERE report_type='GL'")
    gl_through = scalar("SELECT MAX(txn_date) FROM transactions")
    inv = conn.execute(
        "SELECT MAX(date) AS d, COUNT(*) AS n FROM invoice_payments"
    ).fetchone()
    bills = conn.execute(
        "SELECT MAX(date) AS d, COUNT(*) AS n FROM bills_payments"
    ).fetchone()

    buckets = [
        {"key": "COA", "label": "Chart of Accounts",
         "loaded": scalar("SELECT COUNT(*) FROM accounts") or 0,
         "loaded_label": f"{scalar('SELECT COUNT(*) FROM accounts')} accounts",
         "greyed": False, "warn": False},
        {"key": "GL", "label": "General Ledger",
         "loaded_label": f"through {gl_through}" if gl_through else "none",
         "period_end": gl_end, "greyed": False, "warn": False},
        {"key": "AR_AGING", "label": "A/R Aging Detail",
         "as_of": ar_as_of, "stale_days": ar_stale,
         "loaded_label": f"as of {ar_as_of}" if ar_as_of else "none",
         "greyed": False, "warn": bool(ar_stale and ar_stale > STALE_AGING_DAYS)},
        {"key": "AP_AGING", "label": "A/P Aging Detail",
         "as_of": ap_as_of, "stale_days": ap_stale,
         "loaded_label": f"as of {ap_as_of}" if ap_as_of else "none",
         "greyed": greyed,
         "warn": bool(ap_stale and ap_stale > STALE_AGING_DAYS)},
        {"key": "INVOICES_PAYMENTS", "label": "Invoices & Payments",
         "loaded_label": f"{inv['n']} rows through {inv['d']}"
         if inv["n"] else "none", "greyed": False, "warn": False},
        {"key": "BILLS_PAYMENTS", "label": "Bills & Payments",
         "loaded_label": f"{bills['n']} rows through {bills['d']}"
         if bills["n"] else "none", "greyed": greyed, "warn": False},
    ]
    return {
        "buckets": buckets,
        "queue_count": scalar(_QUEUE_SQL),
        "archetype": archetype,
    }


@route("GET", "/api/status")
def _api_status(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    entry = _registry_entry(server, slug)
    if entry is None:
        raise ValueError("Pick a client to see its status.")
    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        status = _build_status(conn)
    finally:
        conn.close()
    status["ok"] = True
    status["last_dashboard"] = entry.get("last_dashboard")
    handler.send_json(status)


def _kpi_value(kpis, key):
    for k in kpis:
        if k.key == key:
            return k.value
    return None


@route("POST", "/api/run")
def _api_run(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    entry = _registry_entry(server, slug)
    if entry is None:
        raise ValueError("Pick a client before updating the dashboard.")

    conn = db.get_client_db(slug, base_dir=server.base_dir)
    global_conn = db.get_global_db(base_dir=server.base_dir)
    try:
        classify_client(conn, global_conn)
        liquidity = compute_liquidity(conn)
        compute_revenue(conn)
        compute_receivables(conn)
        compute_disbursements(conn)
        for scn in SCENARIO_KEYS:
            compute_forecast(conn, scn)
        base = compute_forecast(conn, "BASE")
        save_forecast(conn, base)
        compute_variance(conn)

        out_dir = _client_dir(server, slug) / "dashboards"
        path = generate_dashboard(conn, entry["name"], out_dir, "BASE")
        banner = _change_banner(conn)
        queue_count = conn.execute(_QUEUE_SQL).fetchone()[0]
    finally:
        conn.close()
        global_conn.close()

    dashboard_path = str(path.resolve())
    registry = db.load_registry(base_dir=server.base_dir)
    for client in registry.get("clients", []):
        if client.get("slug") == slug:
            client["last_dashboard"] = dashboard_path
    db.save_registry(registry, base_dir=server.base_dir)

    handler.send_json({
        "ok": True,
        "dashboard_path": dashboard_path,
        "headline": {
            "cash": _kpi_value(liquidity, "cash_on_hand"),
            "weeks_of_cash": _kpi_value(liquidity, "weeks_of_cash"),
            "breach_weeks": base.breach_weeks,
        },
        "changes": banner["text"],
        "queue_count": queue_count,
    })


@route("GET", "/dashboard")
def _serve_dashboard(handler: Handler, query: dict) -> None:
    """Serve the client's latest dashboard over HTTP so the browser will open
    it (file:// links are blocked when clicked from an http:// page)."""
    slug = query.get("client")
    entry = _registry_entry(handler.server, slug)
    path = entry.get("last_dashboard") if entry else None
    if not path or not Path(path).exists():
        return handler.send_bytes(
            b"<!DOCTYPE html><html><body style='font-family:sans-serif;"
            b"padding:40px'><p>No dashboard yet. Go back and click "
            b"<b>Update Dashboard</b> first.</p></body></html>",
            _CONTENT_TYPES[".html"], status=404,
        )
    handler.send_bytes(Path(path).read_bytes(), _CONTENT_TYPES[".html"])


# ── mapping (account classification review) ──────────────────────────────────

_TAXONOMY_CACHE: list | None = None


def _taxonomy() -> dict[str, dict]:
    global _TAXONOMY_CACHE
    if _TAXONOMY_CACHE is None:
        with open(DATA_DIR / "taxonomy.json", encoding="utf-8") as f:
            _TAXONOMY_CACHE = json.load(f)
    return {c["code"]: c for c in _TAXONOMY_CACHE}


def _category_options() -> list[dict]:
    taxonomy = _taxonomy()
    # Preserve the taxonomy file's order (already grouped sensibly).
    return [
        {"code": c["code"], "label": c["label"],
         "explanation": c["explanation"], "group": c.get("group")}
        for c in (_TAXONOMY_CACHE or [])
    ]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@route("GET", "/api/mapping")
def _api_mapping_get(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    if not slug or _registry_entry(server, slug) is None:
        raise ValueError("Pick a client to review its account mapping.")
    conn = db.get_client_db(slug, base_dir=server.base_dir)
    global_conn = db.get_global_db(base_dir=server.base_dir)
    try:
        # Refresh best-guesses; the classifier never touches confirmed rows.
        classify_client(conn, global_conn)
        rows = conn.execute(
            f"""
            SELECT id, qbo_name, full_path, qbo_type, detail_type, category,
                   confidence, status, dormant, inactive_candidate
            FROM accounts a WHERE {NOT_EXCLUDED}
            ORDER BY qbo_name
            """
        ).fetchall()
    finally:
        conn.close()
        global_conn.close()

    taxonomy = _taxonomy()
    groups: dict[str, list] = {"queue": [], "confirm": [], "auto": [],
                               "ignored": []}
    for r in rows:
        info = taxonomy.get(r["category"], {})
        # Inactive/deleted accounts never block review — they're parked in
        # their own group and ignored by the dashboard and proposal.
        tier = "ignored" if r["inactive_candidate"] else \
            tier_for(r["category"], r["confidence"])
        groups[tier].append({
            "id": r["id"],
            "qbo_name": r["qbo_name"],
            "full_path": r["full_path"],
            "qbo_type": r["qbo_type"],
            "detail_type": r["detail_type"],
            "category": r["category"],
            "confidence": r["confidence"],
            "status": r["status"],
            "label": info.get("label"),
            "explanation": info.get("explanation"),
            "dormant": bool(r["dormant"]),
            "inactive": bool(r["inactive_candidate"]),
        })
    handler.send_json({
        "ok": True,
        "groups": groups,
        "counts": {k: len(v) for k, v in groups.items()},
        "categories": _category_options(),
    })


def _audit_user(conn, entity_id, field, old, new, now) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, entity, entity_id, field, old_value, "
        "new_value, source) VALUES (?, 'accounts', ?, ?, ?, ?, 'user')",
        (now, entity_id, field,
         json.dumps(old) if old is not None else None,
         json.dumps(new) if new is not None else None),
    )


@route("POST", "/api/mapping")
def _api_mapping_post(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    if not slug or _registry_entry(server, slug) is None:
        raise ValueError("Pick a client before saving its mapping.")
    payload = json.loads(handler.read_body() or b"{}")
    changes = payload.get("changes") or []
    confirm_all = bool(payload.get("confirm_all"))
    unsure = payload.get("unsure") or []
    remember = payload.get("remember") or []
    ignore = payload.get("ignore") or []
    valid = set(_taxonomy())
    now = _now_iso()

    confirmed = bulk_confirmed = flagged = remembered = ignored = 0
    remember_rows: list[tuple[str, str]] = []
    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        for change in changes:
            aid = change.get("id")
            category = change.get("category")
            if category not in valid:
                raise ValueError(f"Unknown category {category!r}.")
            old = conn.execute(
                "SELECT category, confidence FROM accounts WHERE id = ?", (aid,)
            ).fetchone()
            if old is None:
                continue
            conn.execute(
                "UPDATE accounts SET category = ?, confidence = 100, "
                "status = 'confirmed' WHERE id = ?",
                (category, aid),
            )
            _audit_user(
                conn, aid, "category",
                {"category": old["category"], "confidence": old["confidence"]},
                {"category": category, "confidence": 100}, now,
            )
            confirmed += 1

        if confirm_all:
            cur = conn.execute(
                "UPDATE accounts SET status = 'confirmed' "
                "WHERE status = 'proposed' AND confidence >= 70 "
                "AND NOT (dormant = 1 AND (coa_balance IS NULL OR "
                "coa_balance = 0))"
            )
            bulk_confirmed = cur.rowcount
            if bulk_confirmed:
                _audit_user(
                    conn, None, "status", None,
                    {"confirmed": bulk_confirmed}, now,
                )

        for aid in unsure:
            cur = conn.execute(
                "UPDATE accounts SET status = 'unsure' WHERE id = ?", (aid,)
            )
            flagged += cur.rowcount

        for aid in ignore:
            cur = conn.execute(
                "UPDATE accounts SET inactive_candidate = 1 WHERE id = ?",
                (aid,)
            )
            ignored += cur.rowcount

        for rem in remember:
            account = conn.execute(
                "SELECT full_path, qbo_name FROM accounts WHERE id = ?",
                (rem.get("id"),),
            ).fetchone()
            if account is None or rem.get("category") not in valid:
                continue
            leaf = (
                account["full_path"] or account["qbo_name"]
            ).split(":")[-1].strip().lower()
            remember_rows.append((leaf, rem["category"]))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if remember_rows:
        global_conn = db.get_global_db(base_dir=server.base_dir)
        try:
            for leaf, category in remember_rows:
                existing = global_conn.execute(
                    "SELECT id FROM aliases WHERE pattern = ? AND "
                    "source = 'learned'", (leaf,)
                ).fetchone()
                if existing:
                    global_conn.execute(
                        "UPDATE aliases SET category = ?, confidence = 95, "
                        "origin_client = ? WHERE id = ?",
                        (category, slug, existing["id"]),
                    )
                else:
                    global_conn.execute(
                        "INSERT INTO aliases (pattern, category, confidence, "
                        "source, origin_client, created_at) "
                        "VALUES (?, ?, 95, 'learned', ?, ?)",
                        (leaf, category, slug, now),
                    )
                remembered += 1
            global_conn.commit()
        finally:
            global_conn.close()

    handler.send_json({
        "ok": True,
        "confirmed": confirmed,
        "confirmed_all": bulk_confirmed,
        "unsure": flagged,
        "remembered": remembered,
        "ignored": ignored,
    })


# ── client config + settings ─────────────────────────────────────────────────

_SCENARIO_NUMERIC = {"lag_shift_weeks", "haircut_61_90", "haircut_91",
                     "discretionary"}
_WHO = {"user": "you", "classifier": "auto-classify", "import": "import",
        "system": "system"}


def _as_float(value, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a number for the {label}.")


def _validate_billing(raw) -> str | None:
    """Validate a manual billing schedule; return a canonical JSON string or
    None to clear it. Raises ValueError with a human message on bad input."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(
                "The billing schedule must be valid JSON, e.g. "
                '[{"week": 1, "amount": 5000}].'
            )
    else:
        parsed = raw
    if not isinstance(parsed, list):
        raise ValueError(
            'The billing schedule must be a list, e.g. [{"week": 1, '
            '"amount": 5000}].'
        )
    clean = []
    for entry in parsed:
        if not isinstance(entry, dict) or "week" not in entry \
                or "amount" not in entry:
            raise ValueError(
                'Each billing entry needs a "week" and an "amount", e.g. '
                '{"week": 1, "amount": 5000}.'
            )
        try:
            week = int(entry["week"])
            amount = float(entry["amount"])
        except (TypeError, ValueError):
            raise ValueError("Billing weeks and amounts must be numbers.")
        if not 1 <= week <= 13:
            raise ValueError("Billing weeks must be between 1 and 13.")
        clean.append({"week": week, "amount": amount})
    return json.dumps(clean)


@route("POST", "/api/config")
def _api_config(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    if not slug or _registry_entry(server, slug) is None:
        raise ValueError("Pick a client before saving settings.")
    payload = json.loads(handler.read_body() or b"{}")
    written: dict = {}

    if "display_name" in payload:
        name = (payload["display_name"] or "").strip()
        if not name:
            raise ValueError("Enter a display name for the client.")
        registry = db.load_registry(base_dir=server.base_dir)
        for client in registry.get("clients", []):
            if client.get("slug") == slug:
                client["name"] = name
        db.save_registry(registry, base_dir=server.base_dir)
        written["display_name"] = name

    conn = db.get_client_db(slug, base_dir=server.base_dir)

    def put(key, value):
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        written[key] = value

    def clear(key):
        conn.execute("DELETE FROM config WHERE key = ?", (key,))
        written[key] = None

    try:
        if "cash_floor" in payload:
            put("cash_floor", str(_as_float(payload["cash_floor"], "cash floor")))
        if "archetype" in payload:
            value = payload["archetype"]
            if value in ("ap_driven", "direct_pay"):
                put("archetype", value)
            elif value in (None, "", "auto"):
                clear("archetype")
            else:
                raise ValueError(
                    "Archetype must be ap_driven, direct_pay, or auto."
                )
        if "manual_billing_schedule" in payload:
            normalized = _validate_billing(payload["manual_billing_schedule"])
            if normalized is None:
                clear("manual_billing_schedule")
            else:
                put("manual_billing_schedule", normalized)
        if "scenario" in payload:
            for scenario, params in (payload["scenario"] or {}).items():
                if scenario not in SCENARIO_KEYS:
                    raise ValueError(f"Unknown scenario {scenario!r}.")
                for param, value in params.items():
                    key = f"scenario.{scenario}.{param}"
                    if param in _SCENARIO_NUMERIC:
                        put(key, str(_as_float(value, param)))
                    elif param == "draws":
                        if value not in ("as_detected", "paused"):
                            raise ValueError(
                                "Draws must be as_detected or paused."
                            )
                        put(key, value)
                    else:
                        raise ValueError(f"Unknown scenario setting {param!r}.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    handler.send_json({"ok": True, "config": written})


@route("GET", "/api/config")
def _api_config_get(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    entry = _registry_entry(server, slug)
    if entry is None:
        raise ValueError("Pick a client to load its settings.")
    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        raw = {r["key"]: r["value"]
               for r in conn.execute("SELECT key, value FROM config")}
    finally:
        conn.close()
    scenario: dict = {}
    for key, value in raw.items():
        if key.startswith("scenario."):
            _, scn, param = key.split(".", 2)
            scenario.setdefault(scn, {})[param] = value
    handler.send_json({
        "ok": True,
        "config": {
            "display_name": entry["name"],
            "cash_floor": raw.get("cash_floor"),
            "archetype": raw.get("archetype"),
            "manual_billing_schedule": raw.get("manual_billing_schedule"),
            "scenario": scenario,
        },
    })


@route("GET", "/api/audit")
def _api_audit(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    if not slug or _registry_entry(server, slug) is None:
        raise ValueError("Pick a client to see its history.")
    try:
        limit = max(1, min(200, int(query.get("limit", 50))))
    except ValueError:
        limit = 50
    taxonomy = _taxonomy()

    def label(code):
        if code is None:
            return "unmapped"
        info = taxonomy.get(code)
        return info["label"] if info else code

    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        names = {r["id"]: r["qbo_name"]
                 for r in conn.execute("SELECT id, qbo_name FROM accounts")}
        rows = conn.execute(
            "SELECT ts, entity, entity_id, field, old_value, new_value, source "
            "FROM audit_log WHERE field IN ('category', 'status') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        who = _WHO.get(r["source"], r["source"] or "system")
        when = r["ts"][:10] if r["ts"] else ""
        try:
            when = dt.date.fromisoformat(r["ts"][:10]).strftime("%b %d")
        except (ValueError, TypeError):
            pass
        if r["field"] == "category":
            old = json.loads(r["old_value"]) if r["old_value"] else {}
            new = json.loads(r["new_value"]) if r["new_value"] else {}
            name = names.get(r["entity_id"], f"Account #{r['entity_id']}")
            text = (f"{name} moved from {label(old.get('category'))} to "
                    f"{label(new.get('category'))} — {who}, {when}")
        else:  # status (e.g. confirm-all summary)
            new = json.loads(r["new_value"]) if r["new_value"] else {}
            count = new.get("confirmed")
            text = (f"{count} accounts confirmed — {who}, {when}"
                    if count else f"Status updated — {who}, {when}")
        items.append({"text": text, "source": r["source"], "when": when})
    handler.send_json({"ok": True, "items": items})


@route("GET", "/api/history")
def _api_history(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    if not slug or _registry_entry(server, slug) is None:
        raise ValueError("Pick a client to see its upload history.")
    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        rows = conn.execute(
            "SELECT report_type, period_start, period_end, as_of_date, "
            "filename, row_count, uploaded_at, "
            "(superseded_data IS NOT NULL) AS superseded "
            "FROM uploads ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    uploads = [{
        "report_type": r["report_type"],
        "coverage": (f"as of {r['as_of_date']}" if r["as_of_date"]
                     else (f"{r['period_start']} – {r['period_end']}"
                           if r["period_start"] else "")),
        "filename": r["filename"],
        "row_count": r["row_count"],
        "uploaded_at": r["uploaded_at"],
        "superseded_prior": bool(r["superseded"]),
    } for r in rows]
    handler.send_json({"ok": True, "uploads": uploads})


@route("POST", "/api/proposal")
def _api_proposal(handler: Handler, query: dict) -> None:
    slug = query.get("client")
    server = handler.server
    entry = _registry_entry(server, slug)
    if entry is None:
        raise ValueError("Pick a client before generating a proposal.")
    from core.coa_proposal import write_proposal_files

    conn = db.get_client_db(slug, base_dir=server.base_dir)
    try:
        out_dir = _client_dir(server, slug) / "proposals"
        files = write_proposal_files(conn, out_dir)
    finally:
        conn.close()
    handler.send_json({
        "ok": True,
        "html_path": str(files["html"]),
        "csv_path": str(files["csv"]),
        "counts": files["result"].counts,
    })
