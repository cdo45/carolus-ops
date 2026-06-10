"""QBO client tests: pagination assembly, 401-refresh-retry, backoff.

All HTTP and token access mocked — no live calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from sync import qbo_client, tokens
from sync.qbo_client import (
    PAGE_SIZE,
    QboAuthError,
    QboClient,
    QboRateLimited,
    QboRequestError,
    QboServerError,
    base_url_for,
)

CLIENT_ID = uuid4()
REALM = "9341450000000"


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict[str, Any] | None = None
    text: str = ""

    def json(self) -> dict[str, Any] | None:
        return self.payload


@dataclass
class Transport:
    """Scripted HTTP responses + recorded calls, token fetches, and sleeps."""

    responses: list[FakeResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    token_calls: list[bool] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "method": "GET", **kwargs})
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "method": "POST", **kwargs})
        return self.responses.pop(0)

    def token(self, conn: Any, client_id: Any, *, force_refresh: bool = False) -> str:
        self.token_calls.append(force_refresh)
        return f"tok-{len(self.token_calls)}"


def make_client(
    monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]
) -> tuple[QboClient, Transport]:
    transport = Transport(responses=responses)
    monkeypatch.setattr(qbo_client.requests, "get", transport.get)
    monkeypatch.setattr(qbo_client.requests, "post", transport.post)
    monkeypatch.setattr(tokens, "get_valid_access_token", transport.token)
    client = QboClient(
        conn=object(),  # type: ignore[arg-type]
        client_id=CLIENT_ID,
        realm_id=REALM,
        environment="sandbox",
        sleeper=transport.sleeps.append,
    )
    return client, transport


def query_response(entity: str, rows: list[dict[str, Any]]) -> FakeResponse:
    return FakeResponse(payload={"QueryResponse": {entity: rows}})


def test_base_urls() -> None:
    assert base_url_for("sandbox").startswith("https://sandbox-quickbooks")
    assert base_url_for("production") == "https://quickbooks.api.intuit.com"
    with pytest.raises(ValueError, match="QBO_ENVIRONMENT"):
        base_url_for("staging")


def test_query_paginates_until_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qbo_client, "PAGE_SIZE", 3)
    pages = [
        query_response("Invoice", [{"Id": str(i)} for i in (1, 2, 3)]),
        query_response("Invoice", [{"Id": str(i)} for i in (4, 5, 6)]),
        query_response("Invoice", [{"Id": "7"}]),
    ]
    client, transport = make_client(monkeypatch, pages)

    rows = client.query("Invoice")

    assert [r["Id"] for r in rows] == ["1", "2", "3", "4", "5", "6", "7"]
    statements = [c["params"]["query"] for c in transport.calls]
    assert "STARTPOSITION 1 MAXRESULTS 3" in statements[0]
    assert "STARTPOSITION 4 MAXRESULTS 3" in statements[1]
    assert "STARTPOSITION 7 MAXRESULTS 3" in statements[2]


def test_query_single_short_page_and_where(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch, [query_response("Account", [{"Id": "1"}])]
    )
    rows = client.query("Account", where="Active = true")
    assert len(rows) == 1
    statement = transport.calls[0]["params"]["query"]
    assert statement.startswith("SELECT * FROM Account WHERE Active = true")
    assert f"MAXRESULTS {PAGE_SIZE}" in statement


def test_401_forces_one_refresh_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch,
        [FakeResponse(status_code=401), query_response("Account", [{"Id": "1"}])],
    )
    rows = client.query("Account")
    assert len(rows) == 1
    assert transport.token_calls == [False, True], (
        "second token fetch must be force_refresh=True"
    )
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer tok-2"


def test_401_twice_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch, [FakeResponse(status_code=401), FakeResponse(status_code=401)]
    )
    with pytest.raises(QboAuthError):
        client.query("Account")
    assert len(transport.calls) == 2


def test_backoff_recovers_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch,
        [
            FakeResponse(status_code=500),
            FakeResponse(status_code=503),
            query_response("Account", [{"Id": "1"}]),
        ],
    )
    rows = client.query("Account")
    assert len(rows) == 1
    assert transport.sleeps == [1.0, 2.0], "exponential backoff between retries"


def test_backoff_exhaustion_raises_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, transport = make_client(
        monkeypatch, [FakeResponse(status_code=502) for _ in range(5)]
    )
    with pytest.raises(QboServerError):
        client.query("Account")
    assert len(transport.calls) == 5
    assert transport.sleeps == [1.0, 2.0, 4.0, 8.0]


def test_rate_limit_exhaustion_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = make_client(
        monkeypatch, [FakeResponse(status_code=429) for _ in range(5)]
    )
    with pytest.raises(QboRateLimited):
        client.query("Account")


def test_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch, [FakeResponse(status_code=400, text="bad query")]
    )
    with pytest.raises(QboRequestError) as excinfo:
        client.query("Account")
    assert excinfo.value.status_code == 400
    assert len(transport.calls) == 1
    assert transport.sleeps == []


def test_create_posts_and_returns_body(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch, [FakeResponse(payload={"Bill": {"Id": "77"}})]
    )
    body = client.create("Bill", {"VendorRef": {"value": "3"}})
    assert body == {"Bill": {"Id": "77"}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith(f"/v3/company/{REALM}/bill")
    assert call["json"] == {"VendorRef": {"value": "3"}}


def test_create_retries_401_but_not_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = make_client(
        monkeypatch,
        [FakeResponse(status_code=401), FakeResponse(payload={"Bill": {"Id": "1"}})],
    )
    assert client.create("Bill", {})["Bill"]["Id"] == "1"
    assert transport.token_calls == [False, True]

    client2, transport2 = make_client(monkeypatch, [FakeResponse(status_code=502)])
    with pytest.raises(QboServerError, match="not retried"):
        client2.create("Bill", {})
    assert len(transport2.calls) == 1, "a failed POST must never be replayed"
    assert transport2.sleeps == []


def test_cdc_and_report_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    client, transport = make_client(
        monkeypatch,
        [FakeResponse(payload={"CDCResponse": []}), FakeResponse(payload={"Header": {}})],
    )
    cursor = datetime(2026, 6, 1, tzinfo=timezone.utc)
    client.cdc(["Invoice", "Bill"], cursor)
    client.get_report("ProfitAndLoss", {"date_macro": "This Month"})

    cdc_call, report_call = transport.calls
    assert cdc_call["url"].endswith(f"/v3/company/{REALM}/cdc")
    assert cdc_call["params"]["entities"] == "Invoice,Bill"
    assert cdc_call["params"]["changedSince"] == cursor.isoformat()
    assert report_call["url"].endswith(f"/v3/company/{REALM}/reports/ProfitAndLoss")
