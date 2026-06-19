"""AUTH-1 — incomplete service-token scopes must resolve safely.

A service token's (role, org) ride in ``AccessToken.scopes`` (see
``service_token_verifier``). The org-pin middleware is the boundary
that enforces "one token, one org". This module pins what happens
when the scopes are *incomplete* — ``SCOPE_SVC`` present but no
``SCOPE_ORG_PREFIX`` entry:

- The verifier structurally cannot mint such a token (org_id is a
  required field on ``ServiceTokenRecord`` and is always emitted) —
  proven below so the no-org case can only arise from a future
  refactor / a different minting path.
- The org-pin middleware, however, discriminates "is this a service
  token?" purely on ``pinned_org_from_auth_scopes(...) is not None``.
  A ``[SCOPE_SVC]``-only or ``[SCOPE_SVC, role]``-only token therefore
  takes the "human auth — untouched" branch and is passed through
  *unpinned*. The intended contract is that a service identity with
  no resolvable org must NOT bypass org isolation — it must be
  rejected. That guardrail is the [BUG?] spec below.
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
    SCOPE_ORG_PREFIX,
    SCOPE_ROLE_PREFIX,
    SCOPE_SVC,
    is_service_token_auth,
    pinned_org_from_auth_scopes,
)
from mcpolis.domain.ports import MULTI_ORG_SENTINEL
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.middleware.service_token_pin import (
    ServiceTokenOrgPinMiddleware,
)


def make_service(tmp_path: Path) -> ServiceTokenService:
    return ServiceTokenService(repo=FileServiceTokenRepository(tmp_path))


def make_partial_svc_user(scopes: list[str]) -> AuthenticatedUser:
    """A service-identity AccessToken with caller-chosen scopes.

    Used to forge the malformed tokens the verifier can't actually
    mint, so the middleware's defense-in-depth can be exercised.
    """
    return AuthenticatedUser(
        AccessToken(
            token="svct_forged",
            client_id="svc:ci-bot",
            scopes=scopes,
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


# --- Invariant proof: the verifier always emits all three scopes ---


@pytest.mark.asyncio
async def test_verifier_always_emits_org_scope_for_minted_token(
    tmp_path: Path,
) -> None:
    """No-bug leg: a real verifier can't produce an org-less service
    token. ``ServiceTokenRecord.org_id`` is required and the verifier
    unconditionally appends ``SCOPE_ORG_PREFIX + record.org_id``, so
    the malformed-scope inputs the middleware tests below forge can
    only come from a future minting path, never from this one."""
    service = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-a", label="ci-bot", role_name="reader",
        created_by="admin@example.com",
    )
    access = await ServiceTokenVerifier(service).verify_token(
        minted.raw_token,
    )
    assert access is not None
    assert is_service_token_auth(access.scopes)
    assert pinned_org_from_auth_scopes(access.scopes) == "org-a"
    # Every service-token scope list carries exactly the org scope.
    org_scopes = [
        s for s in access.scopes if s.startswith(SCOPE_ORG_PREFIX)
    ]
    assert org_scopes == [SCOPE_ORG_PREFIX + "org-a"]


# --- Defense-in-depth: middleware on a forged org-less service token ---


@pytest.mark.asyncio
async def test_svc_token_without_org_scope_is_rejected_not_passed_through(
) -> None:
    """[BUG?] Intended contract: a service identity (``SCOPE_SVC``)
    with no resolvable pinned org must NOT reach the inner app
    unpinned — that would bypass org isolation. A bare ``/mcp``
    request (``MULTI_ORG_SENTINEL``) from such a token must be
    rejected (or otherwise never forwarded with the sentinel intact),
    never silently treated as human fan-out."""
    status, _, inner = await run_middleware(
        auth_user=make_partial_svc_user([SCOPE_SVC]),
        org_id=MULTI_ORG_SENTINEL,
    )
    # Must not have reached the inner app with the unresolved sentinel
    # org — that is the org-isolation bypass.
    assert inner.calls != [MULTI_ORG_SENTINEL]
    assert status == 401


@pytest.mark.asyncio
async def test_svc_token_with_role_but_no_org_scope_is_rejected(
) -> None:
    """[BUG?] Same hazard with a role present but org missing —
    ``[SCOPE_SVC, role:reader]``. ``pinned_org_from_auth_scopes``
    still returns None, so the middleware can't tell this apart from
    human auth and forwards it unpinned."""
    status, _, inner = await run_middleware(
        auth_user=make_partial_svc_user(
            [SCOPE_SVC, SCOPE_ROLE_PREFIX + "reader"],
        ),
        org_id=MULTI_ORG_SENTINEL,
    )
    assert inner.calls != [MULTI_ORG_SENTINEL]
    assert status == 401


@pytest.mark.asyncio
async def test_org_less_svc_token_is_rejected_before_reaching_inner_app(
) -> None:
    """Post-fix (AUTH-1): an org-less service token is rejected at the
    org-pin boundary and never reaches the inner app at all. Previously
    this pinned the defective pass-through (status 200, sentinel org
    forwarded); it was flipped in lockstep with the fix to assert the
    fail-closed behavior."""
    status, _, inner = await run_middleware(
        auth_user=make_partial_svc_user([SCOPE_SVC]),
        org_id=MULTI_ORG_SENTINEL,
    )
    assert status == 401
    assert inner.calls == []
