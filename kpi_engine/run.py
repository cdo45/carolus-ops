"""QBO KPI Dashboard — entry point.

Local-first tool that parses QuickBooks Online report exports (CSV/XLSX),
stores them in per-client SQLite databases, computes KPIs, and generates
self-contained offline HTML dashboards.

Running this starts the localhost app on 127.0.0.1 (first free port in
8400-8499), opens it in the default browser, and shuts down after 30 minutes
of inactivity.
"""

import argparse
import platform
import subprocess
import sys
import webbrowser
from pathlib import Path

from app.server import make_server, start_idle_watchdog
from core.db import appdata_root, resource_path

VERSION_FILE = resource_path("VERSION")


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def ensure_appdata() -> Path:
    """Create the .appdata directory on first run; hide it on Windows.

    Returns an absolute path so generated file paths (dashboards, proposals)
    are openable from anywhere, not relative to the launch directory.
    """
    root = appdata_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        try:  # dot-names already hide on mac/linux; Windows needs the flag.
            subprocess.run(["attrib", "+h", str(root)], check=False)
        except Exception:
            pass
    return root


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="QBO KPI Dashboard")
    parser.add_argument(
        "--version", action="version",
        version=f"QBO KPI Dashboard {read_version()}",
    )
    parser.parse_args(argv)

    server = make_server(ensure_appdata())
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    start_idle_watchdog(server)
    print(f"QBO KPI Dashboard {read_version()} running at {url}")
    print("Idle shutdown after 30 minutes. Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
