"""Unit tests for ``DashboardGoogleProvider``.

Covers the slice of the dashboard browser-login flow that talks to
Google: building the authorize-URL and exchanging the callback code
for an email. The membership / cookie / analytics logic stays in
``dashboard_auth.py`` and is covered by ``test_dashboard_api.py``.
"""
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mcpolis.adapters.auth.google_oauth_provider import (
    GOOGLE_AUTH_URL,
    GOOGLE_TOKEN_URL,
    DashboardGoogleProvider,
    extract_email_from_id_token,
)


def make_provider() -> DashboardGoogleProvider:
    return DashboardGoogleProvider(
        client_id="client-abc",
        client_secret="shh",
    )


def make_id_token(email: str) -> str:
    """Mint a Google-style JWT (header.payload.sig) carrying ``email``."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email}).encode(),
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fake-sig"


@pytest.mark.asyncio
async def test_start_login_builds_google_authorize_url() -> None:
    provider = make_provider()
    url = await provider.start_login(
        state="state-xyz",
        redirect_uri="https://example.test/api/auth/callback",
        join=None,
    )

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTH_URL
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["client-abc"]
    assert qs["redirect_uri"] == ["https://example.test/api/auth/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == ["openid email"]
    assert qs["state"] == ["state-xyz"]
    assert qs["prompt"] == ["select_account"]


@pytest.mark.asyncio
async def test_start_login_ignores_join_param() -> None:
    """``join`` is the dashboard's concern (held in pending-state dict);
    Google has no use for it. Sanity-check that it doesn't leak into
    the authorize URL."""
    provider = make_provider()
    url = await provider.start_login(
        state="s",
        redirect_uri="https://example.test/cb",
        join="acme",
    )
    qs = parse_qs(urlparse(url).query)
    assert "join" not in qs
    assert "acme" not in url


@pytest.mark.asyncio
async def test_complete_login_returns_email_from_id_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_provider()
    id_token = make_id_token("alice@example.test")

    posted: dict[str, object] = {}

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

        async def post(self, url: str, data: dict[str, str]) -> FakeResponse:
            posted["url"] = url
            posted["data"] = data
            return FakeResponse()

    monkeypatch.setattr(
        "mcpolis.adapters.auth.google_oauth_provider.httpx.AsyncClient",
        lambda: FakeClient(),
    )

    completed = await provider.complete_login(
        code="auth-code-1",
        state="ignored",
        redirect_uri="https://example.test/api/auth/callback",
    )

    assert completed.email == "alice@example.test"
    assert completed.raw_id_token == id_token
    assert posted["url"] == GOOGLE_TOKEN_URL
    sent = posted["data"]
    assert isinstance(sent, dict)
    assert sent["code"] == "auth-code-1"
    assert sent["client_id"] == "client-abc"
    assert sent["client_secret"] == "shh"
    assert sent["grant_type"] == "authorization_code"


@pytest.mark.asyncio
async def test_complete_login_raises_when_token_endpoint_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_provider()

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

    with pytest.raises(ValueError, match="Failed to exchange code"):
        await provider.complete_login(
            code="bad",
            state="s",
            redirect_uri="https://example.test/cb",
        )


@pytest.mark.asyncio
async def test_complete_login_raises_value_error_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-8: a transport-level fault talking to Google (timeout, connect
    refused) must surface as the clean ValueError the callback route maps
    to a 400 — never a raw httpx exception escaping as an unhandled 500."""
    provider = make_provider()

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, _url: str, data: dict[str, str]) -> object:
            del data
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(
        "mcpolis.adapters.auth.google_oauth_provider.httpx.AsyncClient",
        lambda: FakeClient(),
    )

    with pytest.raises(ValueError, match="Failed to exchange code"):
        await provider.complete_login(
            code="c", state="s", redirect_uri="https://example.test/cb",
        )


@pytest.mark.asyncio
async def test_complete_login_raises_value_error_on_malformed_200_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-8: a 200 with a non-JSON body must also map to the clean
    ValueError, not let a raw json.JSONDecodeError ride out on the route's
    ``except ValueError``."""
    provider = make_provider()

    class FakeResponse:
        status_code = 200
        text = "not json"

        def json(self) -> dict[str, str]:
            return json.loads("<not json>")  # raises JSONDecodeError

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

    with pytest.raises(ValueError, match="Failed to exchange code"):
        await provider.complete_login(
            code="c", state="s", redirect_uri="https://example.test/cb",
        )


@pytest.mark.asyncio
async def test_complete_login_raises_when_id_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_provider()

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"access_token": "lonely"}

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

    with pytest.raises(ValueError, match="did not return an ID token"):
        await provider.complete_login(
            code="ok",
            state="s",
            redirect_uri="https://example.test/cb",
        )


@pytest.mark.asyncio
async def test_complete_login_raises_when_id_token_has_no_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_provider()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "123"}).encode()).rstrip(b"=").decode()
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    no_email_token = f"{header}.{payload}.fake"

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"id_token": no_email_token}

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

    with pytest.raises(ValueError, match="extract email"):
        await provider.complete_login(
            code="ok",
            state="s",
            redirect_uri="https://example.test/cb",
        )


def test_extract_email_from_id_token_handles_padding() -> None:
    """JWT base64 segments may have lengths that are not multiples of
    four — the helper has to add padding before decoding. Build a
    payload whose base64 length is 9 (== 1 mod 4) to exercise the
    padding branch."""
    raw = json.dumps({"email": "x@y.test", "pad": "abc"}).encode()
    payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    header = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    token = f"{header}.{payload}.sig"

    assert extract_email_from_id_token(token) == "x@y.test"


def test_extract_email_from_id_token_returns_none_for_malformed() -> None:
    assert extract_email_from_id_token("not.a.jwt.too.many.dots") is None
    assert extract_email_from_id_token("only-one-segment") is None


# ``httpx`` is imported at module scope in the adapter; keep this
# reference so unused-import warnings stay quiet if the test file's
# imports are pruned.
_ = httpx
