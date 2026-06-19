"""BUG-8 route-level guardrail: the gateway Google-callback route maps a
provider ValueError (the family handle_google_callback now raises for a
token-endpoint fault) to a clean 403 — NOT an unhandled 500.

The provider-level tests prove the fault is *raised* as a ValueError; this
proves the ROUTE *renders* it cleanly, closing the "does the route still
catch it?" gap.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.applications import Starlette

from mcpolis.entrypoints.routes.google_callback import (
    create_google_callback_route,
)


class _ValueErrorProvider:
    """Stands in for McpGatewayOAuthProvider: its callback raises the
    ValueError family a token-endpoint fault now maps to."""

    async def handle_google_callback(self, code: str, state: str) -> str:
        del code, state
        raise ValueError("Google token exchange failed; please retry authentication")


class _RedirectProvider:
    async def handle_google_callback(self, code: str, state: str) -> str:
        del code, state
        return "http://localhost:3000/callback?code=abc"


def make_app(provider: object) -> Starlette:
    app = Starlette(routes=[create_google_callback_route()])
    app.state.mcp_gateway_oauth_provider = provider
    return app


def test_callback_route_maps_provider_value_error_to_403_not_500() -> None:
    client = TestClient(
        make_app(_ValueErrorProvider()), raise_server_exceptions=False,
    )
    resp = client.get("/oauth/google/callback?code=c&state=s")
    assert resp.status_code == 403
    assert resp.json()["error"] == "access_denied"


def test_callback_route_success_redirects() -> None:
    """Control: a successful exchange still 302-redirects to the client."""
    client = TestClient(
        make_app(_RedirectProvider()),
        raise_server_exceptions=False,
        follow_redirects=False,
    )
    resp = client.get("/oauth/google/callback?code=c&state=s")
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:3000/callback?code=abc"
