"""AUTH-1 — an org-less service token must fail closed, and a human
token can never be mistaken for a service token.

A service token's (role, org) ride as typed fields of
``ServiceAccessToken`` (see ``service_token_verifier``). The org-pin
middleware is the boundary that enforces "one token, one org":

- The verifier structurally cannot mint an org-less token
  (``ServiceTokenRecord.org_id`` is required) — proven below, so an
  empty org can only arise from a future minting path.
- The middleware rejects an org-less service token instead of
  forwarding it unpinned with the multi-org sentinel.
- A human ``AccessToken`` whose scopes spell out the old service
  encoding (``mcpolis:svc``, ``mcpolis:org:<id>``) — scopes a client
  can request over OAuth — is passed through as human auth, never
  pinned to the org it names.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from mcpolis.adapters.auth.service_token_verifier import ServiceTokenVerifier
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.model.service_token import (
    ServiceAccessToken,
    is_service_token_auth,
    pinned_org_from_access_token,
)
from mcpolis.domain.ports import MULTI_ORG_SENTINEL
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.middleware.service_token_pin import (
    ServiceTokenOrgPinMiddleware,
)


def make_service(tmp_path: Path) -> ServiceTokenService:
    return ServiceTokenService(repo=FileServiceTokenRepository(tmp_path))


def make_orgless_svc_user() -> AuthenticatedUser:
    """A service token with an empty org — a token the verifier can't
    actually mint, so the middleware's defense-in-depth can be
    exercised."""
    return AuthenticatedUser(
        ServiceAccessToken(
            token="svct_forged",
            client_id="svc:ci-bot",
            scopes=[],
            role_name="reader",
            org_id="",
            expires_at=None,
        ),
    )


def make_human_user_with_forged_scopes() -> AuthenticatedUser:
    """A human OAuth token whose client requested the old service-token
    scope encoding."""
    return AuthenticatedUser(
        AccessToken(
            token="oauth-token",
            client_id="member@example.com",
            scopes=["mcpolis:svc", "mcpolis:role:admin", "mcpolis:org:org-b"],
            expires_at=None,
        ),
    )


class _InnerApp:
    """Records the org id observed inside the wrapped app."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls.append(current_org_id.get())
        await send(
            {"type": "http.response.start", "status": 200, "headers": []},
        )
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


# --- Invariant proof: the verifier always sets the org ---


@pytest.mark.asyncio
async def test_verifier_always_sets_org_for_minted_token(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-a", label="ci-bot", role_name="reader",
        created_by="admin@example.com",
    )
    access = await ServiceTokenVerifier(service).verify_token(
        minted.raw_token,
    )
    assert access is not None
    assert is_service_token_auth(access)
    assert pinned_org_from_access_token(access) == "org-a"


# --- Defense-in-depth: middleware on a forged org-less service token ---


@pytest.mark.asyncio
async def test_org_less_svc_token_is_rejected_before_reaching_inner_app(
) -> None:
    status, _, inner = await run_middleware(
        auth_user=make_orgless_svc_user(),
        org_id=MULTI_ORG_SENTINEL,
    )
    assert status == 401
    assert inner.calls == []


# --- Human token with forged service scopes is never pinned ---


@pytest.mark.asyncio
async def test_human_token_with_forged_org_scope_is_not_pinned() -> None:
    """Bare ``/mcp`` keeps the multi-org sentinel (email-based fan-out
    over the human's real memberships), never the org the scope names."""
    status, _, inner = await run_middleware(
        auth_user=make_human_user_with_forged_scopes(),
        org_id=MULTI_ORG_SENTINEL,
    )
    assert status == 200
    assert inner.calls == [MULTI_ORG_SENTINEL]
