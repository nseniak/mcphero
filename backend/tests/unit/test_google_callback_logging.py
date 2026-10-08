"""The Google callback log line never carries the secret in the redirect.

After Google sign-in the gateway redirects the browser either to the
consent page (``?consent=<token>``) or to the client's redirect URI
(``?code=<gateway code>``). Both query values are secrets, and logs are
forwarded to Elastic.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from structlog.testing import capture_logs
from structlog.typing import EventDict

from mcpolis.entrypoints.routes.google_callback import create_google_callback_route

SECRET = "s3cr3t-value-that-must-not-be-logged"


class FakeCallbackProvider:
    """Stands in for the gateway provider: always redirects to one URL."""

    def __init__(self, redirect_url: str) -> None:
        self.redirect_url = redirect_url

    async def handle_google_callback(self, code: str, state: str) -> str:
        del code, state
        return self.redirect_url


def make_app(redirect_url: str) -> Starlette:
    app = Starlette(routes=[create_google_callback_route()])
    app.state.mcp_gateway_oauth_provider = FakeCallbackProvider(redirect_url)
    return app


async def call_callback(app: Starlette) -> tuple[httpx.Response, list[EventDict]]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
    ) as client:
        with capture_logs() as logs:
            response = await client.get(
                "/oauth/google/callback",
                params={"code": "google-code", "state": "google-state"},
            )
    return response, logs


@pytest.mark.asyncio
async def test_the_gateway_code_on_the_client_redirect_is_not_logged() -> None:
    app = make_app(
        f"https://claude.ai/api/mcp/auth_callback?code={SECRET}&state=client-state",
    )

    response, logs = await call_callback(app)

    assert response.status_code == 302
    assert SECRET in response.headers["location"]
    assert SECRET not in repr(logs)
    success = [e for e in logs if e["event"] == "google.oauth.callback.success"]
    assert [e["redirect_to"] for e in success] == [
        "https://claude.ai/api/mcp/auth_callback",
    ]


@pytest.mark.asyncio
async def test_the_consent_token_is_not_logged() -> None:
    app = make_app(f"https://mcphero.io/mcp/oauth/consent?consent={SECRET}")

    response, logs = await call_callback(app)

    assert response.status_code == 302
    assert SECRET not in repr(logs)


@pytest.mark.asyncio
async def test_userinfo_in_the_redirect_is_not_logged() -> None:
    app = make_app(f"https://user:{SECRET}@client.example/cb?code=x")

    _, logs = await call_callback(app)

    assert SECRET not in repr(logs)
