"""Token refresh logic against mocked Intuit responses and a fake DB conn.

Covers: cache hit, expiry refresh, refresh-token ROTATION persistence (the
new pair must be stored before the access token is returned), corrupt
ciphertext self-recovery, and invalid_grant -> NeedsReauth marking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
import requests
from cryptography.fernet import Fernet

from sync import crypto, tokens

CLIENT_ID = uuid4()


@dataclass
class FakeCursor:
    row: tuple[Any, ...] | None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


@dataclass
class FakeConn:
    """Stands in for psycopg.Connection: one SELECT row + recorded writes."""

    select_row: tuple[Any, ...] | None
    executed: list[tuple[str, tuple[Any, ...] | None]] = field(default_factory=list)
    fail_on_update: bool = False
    commits: int = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> FakeCursor:
        self.executed.append((sql, params))
        if "UPDATE" in sql and self.fail_on_update:
            raise RuntimeError("simulated write failure")
        return FakeCursor(self.select_row if "SELECT" in sql else None)

    def commit(self) -> None:
        self.commits += 1

    def updates(self, containing: str) -> list[tuple[str, tuple[Any, ...] | None]]:
        return [e for e in self.executed if "UPDATE" in e[0] and containing in e[0]]


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict[str, Any] | None = None
    text: str = ""

    def json(self) -> dict[str, Any] | None:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


REFRESH_OK = {
    "access_token": "new-access-token",
    "refresh_token": "rotated-refresh-token",
    "expires_in": 3600,
    "x_refresh_token_expires_in": 8726400,
}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("QBO_CLIENT_ID", "qbo-app-id")
    monkeypatch.setenv("QBO_CLIENT_SECRET", "qbo-app-secret")


def conn_row(
    access: str | None = "live-access-token",
    refresh: str | None = "live-refresh-token",
    expires_in: timedelta = timedelta(hours=1),
    status: str = "active",
    encrypt: bool = True,
) -> tuple[Any, ...]:
    enc = crypto.encrypt if encrypt else (lambda value: value)
    return (
        status,
        enc(access) if access is not None else None,
        enc(refresh) if refresh is not None else None,
        datetime.now(timezone.utc) + expires_in,
    )


def post_returning(
    monkeypatch: pytest.MonkeyPatch, response: FakeResponse
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(tokens.requests, "post", fake_post)
    return calls


def test_valid_token_returned_without_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_post(*args: Any, **kwargs: Any) -> FakeResponse:
        raise AssertionError("HTTP must not be called for a fresh token")

    monkeypatch.setattr(tokens.requests, "post", no_post)
    conn = FakeConn(select_row=conn_row())
    token = tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]
    assert token == "live-access-token"
    assert conn.updates("sync_connections") == []


def test_expired_token_refreshes_and_persists_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = post_returning(monkeypatch, FakeResponse(payload=dict(REFRESH_OK)))
    conn = FakeConn(select_row=conn_row(expires_in=timedelta(minutes=-10)))

    token = tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]

    assert token == "new-access-token"
    assert calls[0]["data"]["refresh_token"] == "live-refresh-token"
    persisted = conn.updates("access_token_enc")
    assert len(persisted) == 1, "rotated pair must be persisted exactly once"
    params = persisted[0][1]
    assert params is not None
    # params: (access_enc, refresh_enc, token_expires_at, refresh_expires_at, client_id)
    assert crypto.decrypt(params[0]) == "new-access-token"
    assert crypto.decrypt(params[1]) == "rotated-refresh-token", (
        "the ROTATED refresh token must be stored, not the old one"
    )
    assert conn.commits >= 1


def test_refresh_inside_expiry_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    post_returning(monkeypatch, FakeResponse(payload=dict(REFRESH_OK)))
    conn = FakeConn(select_row=conn_row(expires_in=timedelta(minutes=2)))
    token = tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]
    assert token == "new-access-token"


def test_force_refresh_skips_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    post_returning(monkeypatch, FakeResponse(payload=dict(REFRESH_OK)))
    conn = FakeConn(select_row=conn_row())  # not expired
    token = tokens.get_valid_access_token(conn, CLIENT_ID, force_refresh=True)  # type: ignore[arg-type]
    assert token == "new-access-token"


def test_persist_failure_blocks_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the rotated pair cannot be stored, the caller must NOT get a token."""
    post_returning(monkeypatch, FakeResponse(payload=dict(REFRESH_OK)))
    conn = FakeConn(
        select_row=conn_row(expires_in=timedelta(minutes=-10)), fail_on_update=True
    )
    with pytest.raises(RuntimeError, match="simulated write failure"):
        tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]


def test_corrupt_access_token_self_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage ciphertext in access_token_enc -> refresh path, not a crash."""
    post_returning(monkeypatch, FakeResponse(payload=dict(REFRESH_OK)))
    row = conn_row()  # unexpired, so only the corrupt ciphertext forces refresh
    corrupt_row = (row[0], "corrupted-not-fernet", row[2], row[3])
    conn = FakeConn(select_row=corrupt_row)
    token = tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]
    assert token == "new-access-token"


def test_invalid_grant_marks_needs_reauth(monkeypatch: pytest.MonkeyPatch) -> None:
    post_returning(
        monkeypatch,
        FakeResponse(status_code=400, text='{"error":"invalid_grant"}'),
    )
    conn = FakeConn(select_row=conn_row(expires_in=timedelta(minutes=-10)))
    with pytest.raises(tokens.NeedsReauth):
        tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]
    marked = conn.updates("needs_reauth")
    assert len(marked) == 1, "connection must be marked needs_reauth"


def test_corrupt_refresh_token_raises_needs_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_post(*args: Any, **kwargs: Any) -> FakeResponse:
        raise AssertionError("HTTP must not be called without a usable refresh token")

    monkeypatch.setattr(tokens.requests, "post", no_post)
    row = conn_row(expires_in=timedelta(minutes=-10))
    corrupt_row = (row[0], row[1], "corrupted-not-fernet", row[3])
    conn = FakeConn(select_row=corrupt_row)
    with pytest.raises(tokens.NeedsReauth):
        tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]
    assert len(conn.updates("needs_reauth")) == 1


def test_missing_connection_row() -> None:
    conn = FakeConn(select_row=None)
    with pytest.raises(tokens.NoConnection):
        tokens.get_valid_access_token(conn, CLIENT_ID)  # type: ignore[arg-type]
