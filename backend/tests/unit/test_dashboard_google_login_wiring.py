"""HTTP-level wiring test for the Google dashboard provider.

Symmetrical to ``test_dashboard_dev_stub_login.py`` but for the
production path. The provider's authorize-URL / token-exchange
internals already have unit coverage in
``test_dashboard_google_provider.py``; this file asserts that
constructing ``Settings(oauth_provider="google", ...)`` and running
``create_app(...)`` actually wires that provider into the dashboard
router — i.e. ``GET /api/auth/login`` redirects to Google and
``GET /api/auth/callback`` exchanges through ``DashboardGoogleProvider``
and lands a real signed cookie.

We don't hit Google for real — ``httpx.AsyncClient`` is patched at the
adapter module so the token-exchange returns a synthetic ID token.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings

MCP_JSON = json.dumps({"mcpServers": {}})
CONFIG_JSON = json.dumps({
    "upstreams": {},
    "roles": {
        "admin": {"is_admin": True, "settings": {"mcp_access": {"mcps": {}}}},
    },
    "users": {
        "admin@example.com": {"role": "admin"},
    },
})


def make_google_client(tmp_path: Path) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(MCP_JSON)
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="google",
        google_client_id="test-google-client",
        google_client_secret="test-google-secret",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


def make_id_token(email: str) -> str:
    """Mint a Google-shaped JWT (header.payload.sig) carrying ``email``."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "sub": "google-uid-123"}).encode(),
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fake-sig"


def test_login_redirects_to_google_authorize(tmp_path: Path) -> None:
    """``oauth_provider=google`` wired through ``create_app`` lands the
    user on the Google consent screen — not on the dev-stub picker."""
    client = make_google_client(tmp_path)
    resp = client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    parsed = urlparse(location)
    assert parsed.netloc == "accounts.google.com", location
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["test-google-client"]
    assert qs["scope"] == ["openid email"]
    assert qs["redirect_uri"] == ["http://localhost:8000/api/auth/callback"]
    assert "state" in qs


def test_callback_exchanges_code_and_issues_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Walk a real /login → /callback round-trip with the Google
    token endpoint stubbed. The cookie that lands at the end must
    authenticate against the production cookie-verify path
    (i.e. /api/auth/me with no header tricks returns the user)."""
    client = make_google_client(tmp_path)

    # Initiate the round-trip and capture the state token from the
    # Google redirect URL — same way a real browser would.
    login_resp = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_resp.headers["location"]).query)["state"][0]

    # Patch the adapter's httpx so the callback's token-exchange
    # returns our fake id_token without hitting accounts.google.com.
    id_token = make_id_token("admin@example.com")

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"id_token": id_token, "access_token": "ignored"}

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, _url: str, data: dict[str, str]) -> FakeResponse:
            del data
            return FakeResponse()

    monkeypatch.setattr(
        "mcpolis.adapters.auth.google_oauth_provider.httpx.AsyncClient",
        lambda: FakeClient(),
    )

    callback_resp = client.get(
        "/api/auth/callback",
        params={"code": "fake-google-code", "state": state},
        follow_redirects=False,
    )
    assert callback_resp.status_code == 302, callback_resp.text
    set_cookie = callback_resp.headers.get("set-cookie", "")
    assert "mcpolis_session=" in set_cookie
    assert "HttpOnly" in set_cookie

    # The cookie is in the jar; /me should authenticate via the same
    # cookie-verify path the dev-stub tests use.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"
    assert me.json()["is_admin"] is True


def test_callback_rejects_token_exchange_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Google's token endpoint returns an error, /callback must
    400 — not silently issue a cookie and not 5xx the request."""
    client = make_google_client(tmp_path)
    login_resp = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_resp.headers["location"]).query)["state"][0]

    class FakeResponse:
        status_code = 400
        text = "invalid_grant"

        def json(self) -> dict[str, str]:
            return {}

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, _url: str, data: dict[str, str]) -> FakeResponse:
            del data
            return FakeResponse()

    monkeypatch.setattr(
        "mcpolis.adapters.auth.google_oauth_provider.httpx.AsyncClient",
        lambda: FakeClient(),
    )

    resp = client.get(
        "/api/auth/callback",
        params={"code": "bad", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_dev_stub_routes_not_mounted_under_google(tmp_path: Path) -> None:
    """The picker page is provider-owned and only mounted by the
    dev-stub adapter — ``oauth_provider=google`` must not expose it."""
    client = make_google_client(tmp_path)
    resp = client.get(
        "/api/auth/dev-stub/picker",
        params={
            "state": "x",
            "redirect_uri": "http://localhost:8000/api/auth/callback",
        },
    )
    assert resp.status_code == 404
