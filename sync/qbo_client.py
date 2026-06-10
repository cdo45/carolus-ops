"""Thin client for the QuickBooks Online v3 API.

Base URL switches on QBO_ENVIRONMENT (sandbox/production). Every call gets a
fresh access token from sync.tokens; a 401 forces exactly one token refresh
and retry; 429/5xx retry with exponential backoff up to MAX_TRIES attempts.
Errors are typed so callers can branch without parsing strings. Responses
are returned as parsed JSON, untouched — landing them raw is the sync
engine's job.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
import requests

from sync import tokens

BASE_URLS: dict[str, str] = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
PAGE_SIZE: int = 1000  # QBO query maximum
MAX_TRIES: int = 5  # attempts per request for 429/5xx
INITIAL_BACKOFF_SECONDS: float = 1.0
_HTTP_TIMEOUT: float = 90.0


class QboError(Exception):
    """Base for all QBO client errors."""


class QboAuthError(QboError):
    """Still unauthorized after a forced token refresh."""


class QboRateLimited(QboError):
    """429 persisted through all backoff attempts."""


class QboServerError(QboError):
    """5xx persisted through all backoff attempts."""


class QboRequestError(QboError):
    """Non-retryable 4xx from QBO."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"QBO request failed with {status_code}: {body[:500]}")


def base_url_for(environment: str) -> str:
    try:
        return BASE_URLS[environment]
    except KeyError:
        raise ValueError(
            f"QBO_ENVIRONMENT must be one of {sorted(BASE_URLS)}, got {environment!r}"
        ) from None


class QboClient:
    """One client per (db connection, carolus client, QBO realm)."""

    def __init__(
        self,
        conn: psycopg.Connection,
        client_id: UUID,
        realm_id: str,
        *,
        environment: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.conn = conn
        self.client_id = client_id
        self.realm_id = realm_id
        env = environment or os.environ.get("QBO_ENVIRONMENT", "sandbox")
        self.base_url = base_url_for(env)
        self._sleep = sleeper

    # ---------------- endpoints ----------------

    def query(self, entity: str, where: str | None = None) -> list[dict[str, Any]]:
        """Run `SELECT * FROM <entity> [WHERE ...]`, following pagination."""
        results: list[dict[str, Any]] = []
        start = 1
        while True:
            statement = f"SELECT * FROM {entity}"
            if where:
                statement += f" WHERE {where}"
            statement += f" STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
            data = self._request("query", {"query": statement})
            page = data.get("QueryResponse", {}).get(entity, [])
            results.extend(page)
            if len(page) < PAGE_SIZE:
                return results
            start += PAGE_SIZE

    def get_report(
        self, name: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        return self._request(f"reports/{name}", params)

    def cdc(
        self, entities: Sequence[str], changed_since: datetime
    ) -> dict[str, Any]:
        """Change Data Capture: everything in `entities` changed since cursor."""
        return self._request(
            "cdc",
            {
                "entities": ",".join(entities),
                "changedSince": changed_since.isoformat(),
            },
        )

    # ---------------- transport ----------------

    def _request(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}/v3/company/{self.realm_id}/{path}"
        refreshed_once = False
        force_next = False
        backoff_tries = 0
        delay = INITIAL_BACKOFF_SECONDS
        while True:
            token = tokens.get_valid_access_token(
                self.conn, self.client_id, force_refresh=force_next
            )
            force_next = False
            response = requests.get(
                url,
                params=dict(params or {}),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=_HTTP_TIMEOUT,
            )
            status = response.status_code
            if status == 401:
                if refreshed_once:
                    raise QboAuthError(
                        f"client {self.client_id}: still 401 after forced refresh"
                    )
                refreshed_once = True
                force_next = True
                continue
            if status == 429 or status >= 500:
                backoff_tries += 1
                if backoff_tries >= MAX_TRIES:
                    if status == 429:
                        raise QboRateLimited(f"429 after {backoff_tries} tries")
                    raise QboServerError(f"{status} after {backoff_tries} tries")
                self._sleep(delay)
                delay *= 2
                continue
            if status >= 400:
                raise QboRequestError(status, response.text)
            return response.json()
