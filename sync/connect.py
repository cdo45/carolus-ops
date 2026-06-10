"""One-time OAuth connect flow for a QBO company (realm).

Usage:
    uv run python -m sync.connect [--name "Client display name"]

Builds the Intuit authorize URL, opens it (and prints it for headless use),
listens on the redirect URI's localhost port for the callback, exchanges the
code for tokens, upserts the client row by realm id, and stores the tokens
encrypted in sync_connections. Prints the realm id only — never tokens.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

import psycopg
import requests
from dotenv import load_dotenv

from sync import crypto
from sync.tokens import TOKEN_URL, _HTTP_TIMEOUT

AUTHORIZE_URL: str = "https://appcenter.intuit.com/connect/oauth2"
SCOPE: str = "com.intuit.quickbooks.accounting"


@dataclass(frozen=True)
class Callback:
    code: str
    realm_id: str
    state: str


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def parse_callback_path(path: str) -> Callback | None:
    """Extract code/realmId/state from the redirect request path, if present."""
    query = parse_qs(urlparse(path).query)
    try:
        return Callback(
            code=query["code"][0],
            realm_id=query["realmId"][0],
            state=query["state"][0],
        )
    except KeyError:
        return None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        callback = parse_callback_path(self.path)
        status = 200 if callback else 404
        body = (
            b"<html><body>Connected. You can close this tab.</body></html>"
            if callback
            else b"not found"
        )
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)
        if callback:
            self.server.callback = callback

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default request logging — the URL contains the auth code."""


class _CallbackServer(HTTPServer):
    callback: Callback | None = None


def wait_for_callback(host: str, port: int) -> Callback:
    """Serve until the OAuth redirect arrives; return its parameters."""
    with _CallbackServer((host, port), _CallbackHandler) as server:
        while server.callback is None:
            server.handle_request()
        return server.callback


def exchange_code(
    code: str, redirect_uri: str, qbo_client_id: str, qbo_client_secret: str
) -> dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        auth=(qbo_client_id, qbo_client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def store_connection(
    conn: psycopg.Connection,
    realm_id: str,
    name: str,
    token_payload: dict[str, Any],
) -> UUID:
    """Upsert the client by realm id and store encrypted tokens; return client id."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        """
        INSERT INTO clients (name, qbo_realm_id) VALUES (%s, %s)
        ON CONFLICT (qbo_realm_id) DO UPDATE SET qbo_realm_id = excluded.qbo_realm_id
        RETURNING id
        """,
        (name, realm_id),
    ).fetchone()
    assert row is not None
    client_id: UUID = row[0]
    refresh_expires_at = (
        now + timedelta(seconds=int(token_payload["x_refresh_token_expires_in"]))
        if "x_refresh_token_expires_in" in token_payload
        else None
    )
    conn.execute(
        """
        INSERT INTO sync_connections
            (client_id, status, access_token_enc, refresh_token_enc,
             token_expires_at, refresh_expires_at)
        VALUES (%s, 'active', %s, %s, %s, %s)
        ON CONFLICT (client_id) DO UPDATE SET
            status = 'active',
            access_token_enc = excluded.access_token_enc,
            refresh_token_enc = excluded.refresh_token_enc,
            token_expires_at = excluded.token_expires_at,
            refresh_expires_at = excluded.refresh_expires_at
        """,
        (
            client_id,
            crypto.encrypt(str(token_payload["access_token"])),
            crypto.encrypt(str(token_payload["refresh_token"])),
            now + timedelta(seconds=int(token_payload.get("expires_in", 3600))),
            refresh_expires_at,
        ),
    )
    conn.commit()
    return client_id


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Connect a QBO company")
    parser.add_argument("--name", default=None, help="client display name")
    args = parser.parse_args(argv)

    try:
        qbo_client_id = os.environ["QBO_CLIENT_ID"]
        qbo_client_secret = os.environ["QBO_CLIENT_SECRET"]
        redirect_uri = os.environ["QBO_REDIRECT_URI"]
        database_url = os.environ["DATABASE_URL"]
    except KeyError as exc:
        print(f"missing required env var: {exc.args[0]}", file=sys.stderr)
        return 2

    parsed = urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        print("QBO_REDIRECT_URI must point at localhost for this flow", file=sys.stderr)
        return 2

    state = secrets.token_urlsafe(32)
    url = build_authorize_url(qbo_client_id, redirect_uri, state)
    print("Open this URL to authorize (opening browser if available):")
    print(url)
    webbrowser.open(url)

    callback = wait_for_callback(parsed.hostname, parsed.port or 80)
    if callback.state != state:
        print("state mismatch on callback — aborting (possible CSRF)", file=sys.stderr)
        return 1

    token_payload = exchange_code(
        callback.code, redirect_uri, qbo_client_id, qbo_client_secret
    )
    with psycopg.connect(database_url) as conn:
        client_id = store_connection(
            conn,
            realm_id=callback.realm_id,
            name=args.name or f"Realm {callback.realm_id}",
            token_payload=token_payload,
        )
    print(f"connected: realm {callback.realm_id} -> client {client_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
