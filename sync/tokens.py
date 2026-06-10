"""Access-token retrieval with auto-refresh against the Intuit OAuth endpoint.

CRITICAL: Intuit ROTATES refresh tokens — every refresh response carries a
new refresh_token and the old one is not guaranteed to keep working. The
rotated pair is therefore persisted before the access token is returned to
any caller; a crash after refresh but before persist must be impossible.

Token values are never logged or embedded in exceptions — only client ids.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
import requests

from sync import crypto

TOKEN_URL: str = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
# refresh when this close to (or past) access-token expiry
EXPIRY_BUFFER: timedelta = timedelta(minutes=5)
_HTTP_TIMEOUT: float = 30.0


class NeedsReauth(Exception):
    """Refresh token is dead (invalid_grant / missing / undecryptable).

    The connection has been marked needs_reauth; run `uv run python -m
    sync.connect` to issue a fresh consent for this client.
    """


class NoConnection(Exception):
    """Client has no sync_connections row."""


@dataclass(frozen=True)
class _ConnRow:
    status: str
    access_token_enc: str | None
    refresh_token_enc: str | None
    token_expires_at: datetime | None


def _load_connection(conn: psycopg.Connection, client_id: UUID) -> _ConnRow:
    row = conn.execute(
        """
        SELECT status, access_token_enc, refresh_token_enc, token_expires_at
        FROM sync_connections WHERE client_id = %s
        """,
        (client_id,),
    ).fetchone()
    if row is None:
        raise NoConnection(f"no QBO connection for client {client_id}")
    return _ConnRow(
        status=row[0],
        access_token_enc=row[1],
        refresh_token_enc=row[2],
        token_expires_at=row[3],
    )


def _persist_tokens(
    conn: psycopg.Connection, client_id: UUID, payload: dict[str, Any]
) -> None:
    """Store a token-endpoint response (encrypted) and reactivate the connection."""
    now = datetime.now(timezone.utc)
    refresh_expires_at = (
        now + timedelta(seconds=int(payload["x_refresh_token_expires_in"]))
        if "x_refresh_token_expires_in" in payload
        else None
    )
    conn.execute(
        """
        UPDATE sync_connections
        SET status = 'active',
            access_token_enc = %s,
            refresh_token_enc = %s,
            token_expires_at = %s,
            refresh_expires_at = COALESCE(%s, refresh_expires_at)
        WHERE client_id = %s
        """,
        (
            crypto.encrypt(str(payload["access_token"])),
            crypto.encrypt(str(payload["refresh_token"])),
            now + timedelta(seconds=int(payload.get("expires_in", 3600))),
            refresh_expires_at,
            client_id,
        ),
    )
    conn.commit()


def _mark_needs_reauth(conn: psycopg.Connection, client_id: UUID) -> None:
    conn.execute(
        "UPDATE sync_connections SET status = 'needs_reauth' WHERE client_id = %s",
        (client_id,),
    )
    conn.commit()


def get_valid_access_token(
    conn: psycopg.Connection, client_id: UUID, *, force_refresh: bool = False
) -> str:
    """Return a decrypted, currently-valid access token for the client.

    Refreshes when forced, expired, within EXPIRY_BUFFER of expiry, or when
    the stored ciphertext is corrupt (self-recovery via refresh token).
    """
    row = _load_connection(conn, client_id)
    if row.refresh_token_enc is None:
        _mark_needs_reauth(conn, client_id)
        raise NeedsReauth(f"client {client_id}: no refresh token stored")
    if (
        not force_refresh
        and row.access_token_enc
        and row.token_expires_at is not None
        and row.token_expires_at - EXPIRY_BUFFER > datetime.now(timezone.utc)
    ):
        try:
            return crypto.decrypt(row.access_token_enc)
        except crypto.InvalidToken:
            pass  # corrupt at-rest value: fall through and refresh
    return _refresh(conn, client_id, row)


def _refresh(conn: psycopg.Connection, client_id: UUID, row: _ConnRow) -> str:
    try:
        refresh_token = crypto.decrypt(row.refresh_token_enc or "")
    except crypto.InvalidToken as exc:
        _mark_needs_reauth(conn, client_id)
        raise NeedsReauth(
            f"client {client_id}: stored refresh token is undecryptable"
        ) from exc

    response = requests.post(
        TOKEN_URL,
        auth=(os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"]),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Accept": "application/json"},
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code == 400 and "invalid_grant" in response.text:
        _mark_needs_reauth(conn, client_id)
        raise NeedsReauth(
            f"client {client_id}: refresh rejected (invalid_grant) — reconnect"
        )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    # rotation: persist the new pair BEFORE handing out the access token
    _persist_tokens(conn, client_id, payload)
    return str(payload["access_token"])
