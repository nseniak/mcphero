"""ServiceTokenOrgPinMiddleware unit tests against a synthetic ASGI app.

The middleware reads ``auth_context_var`` (service-token scopes) and
``current_org_id`` (set by OrgContextMiddleware in production); both
are set directly here.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from mcpolis.domain.model.service_token import (
    SCOPE_ORG_PREFIX,
    SCOPE_ROLE_PREFIX,
    SCOPE_SVC,
)
from mcpolis.domain.ports import MULTI_ORG_SENTINEL
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
)
from mcpolis.entrypoints.middleware.service_token_pin import (
    ServiceTokenOrgPinMiddleware,
)


def make_svc_user(org_id: str = "org-a", role: str = "reader") -> AuthenticatedUser:
    return AuthenticatedUser(
        AccessToken(
            token="svct_x",
            client_id="svc:ci-bot",
            scopes=[
                SCOPE_SVC,
                SCOPE_ROLE_PREFIX + role,
                SCOPE_ORG_PREFIX + org_id,
            ],
            expires_at=None,
        ),
    )


def make_human_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        AccessToken(
            token="oauth-token",
            client_id="alice@example.com",
            scopes=[],
            expires_at=None,
        ),
    )


class _InnerApp:
    """Records the org id observed inside the wrapped app."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls.append(current_org_id.get())
        await send({
            "type": "http.response.start", "status": 200, "headers": [],
        })
        await send({"type": "http.response.body", "body": b"ok"})


async def run_middleware(
    *,
    auth_user: AuthenticatedUser | None,
    org_id: str,
) -> tuple[int, dict[str, Any] | None, _InnerApp]:
    """Drive one synthetic request; return (status, json_body, inner)."""
    inner = _InnerApp()
    middleware = ServiceTokenOrgPinMiddleware(inner)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        sent.append(dict(message))

    scope = {"type": "http", "path": "/mcp/", "headers": []}
    auth_token = auth_context_var.set(auth_user)
    org_token = current_org_id.set(org_id)
    try:
        await middleware(scope, receive, send)
    finally:
        auth_context_var.reset(auth_token)
        current_org_id.reset(org_token)

    status = next(
        m["status"] for m in sent if m["type"] == "http.response.start"
    )
    body_bytes = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    body: dict[str, Any] | None
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    return status, body, inner


@pytest.mark.asyncio
async def test_sentinel_org_overridden_to_pinned_org() -> None:
    status, _, inner = await run_middleware(
        auth_user=make_svc_user(org_id="org-a"),
        org_id=MULTI_ORG_SENTINEL,
    )
    assert status == 200
    assert inner.calls == ["org-a"]


@pytest.mark.asyncio
async def test_slug_org_mismatch_returns_401_anti_enumeration_body() -> None:
    status, body, inner = await run_middleware(
        auth_user=make_svc_user(org_id="org-a"),
        org_id="org-b",
    )
    assert status == 401
    # Exact same body OrgContextMiddleware uses for unknown slugs, so
    # a token holder can't distinguish "wrong org" from "no such org".
    assert body == {"error": "Not authorized for this org"}
    assert inner.calls == []


@pytest.mark.asyncio
async def test_matching_org_passes_through() -> None:
    status, _, inner = await run_middleware(
        auth_user=make_svc_user(org_id="org-a"),
        org_id="org-a",
    )
    assert status == 200
    assert inner.calls == ["org-a"]


@pytest.mark.asyncio
async def test_human_auth_passes_through_untouched() -> None:
    status, _, inner = await run_middleware(
        auth_user=make_human_user(),
        org_id=MULTI_ORG_SENTINEL,
    )
    assert status == 200
    assert inner.calls == [MULTI_ORG_SENTINEL]


@pytest.mark.asyncio
async def test_unauthenticated_passes_through() -> None:
    status, _, inner = await run_middleware(
        auth_user=None,
        org_id=MULTI_ORG_SENTINEL,
    )
    assert status == 200
    assert inner.calls == [MULTI_ORG_SENTINEL]
