"""Pure-helper tests for the OAuth connect flow (no network, no browser)."""

from urllib.parse import parse_qs, urlparse

from sync.connect import SCOPE, build_authorize_url, parse_callback_path


def test_authorize_url_contents() -> None:
    url = build_authorize_url(
        "app-id", "http://localhost:8000/callback", "state-token"
    )
    parsed = urlparse(url)
    assert parsed.hostname == "appcenter.intuit.com"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["app-id"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [SCOPE]
    assert query["redirect_uri"] == ["http://localhost:8000/callback"]
    assert query["state"] == ["state-token"]


def test_parse_callback_extracts_params() -> None:
    callback = parse_callback_path(
        "/callback?code=abc123&realmId=9341453&state=xyz"
    )
    assert callback is not None
    assert callback.code == "abc123"
    assert callback.realm_id == "9341453"
    assert callback.state == "xyz"


def test_parse_callback_rejects_incomplete() -> None:
    assert parse_callback_path("/callback?code=abc123") is None
    assert parse_callback_path("/favicon.ico") is None
