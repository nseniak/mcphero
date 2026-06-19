"""ServiceTokenOrgPinMiddleware unit tests against a synthetic ASGI app.

The middleware reads ``auth_context_var`` (service-token scopes) and
``current_org_id`` (set by OrgContextMiddleware in production); both
are set directly here.
"""
from __future__ import annotations

import asyncio
import contextvars
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
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
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


# --- AUTH-17: contextvar isolation across concurrent requests ---


class _BarrierInnerApp:
    """Inner app that records the org it observed and parks on a barrier
    so two concurrent requests are *both* inside the app — each having
    set its own ``current_org_id`` — before either unwinds. That is the
    window in which a contextvar leak would surface."""

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self._barrier = barrier
        self.observed_org: str | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.observed_org = current_org_id.get()
        # Block until the sibling request is also inside the app.
        await self._barrier.wait()
        # Re-read after the rendezvous: if the sibling's set/reset
        # bled across tasks, the value would have changed here.
        self.observed_org = current_org_id.get()
        await send(
            {"type": "http.response.start", "status": 200, "headers": []},
        )
        await send({"type": "http.response.body", "body": b"ok"})


async def run_one_request_in_own_context(
    *,
    pinned_org: str,
    inner: _BarrierInnerApp,
) -> str | None:
    """Drive one bare-/mcp service-token request in a *fresh* context
    copy (mirroring one ASGI task), so contextvar isolation is genuinely
    under test rather than masked by a shared call-site context."""
    middleware = ServiceTokenOrgPinMiddleware(inner)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        return None

    scope = {"type": "http", "path": "/mcp/", "headers": []}

    async def driver() -> None:
        # Production order: AuthContextMiddleware sets the auth user and
        # OrgContextMiddleware sets the sentinel org; both live in this
        # request's own context.
        auth_context_var.set(make_svc_user(org_id=pinned_org))
        current_org_id.set(MULTI_ORG_SENTINEL)
        await middleware(scope, receive, send)

    # ``contextvars.copy_context().run`` gives this request an isolated
    # context, exactly like a fresh asyncio task created per request.
    ctx = contextvars.copy_context()
    await ctx.run(driver)  # type: ignore[arg-type]
    return inner.observed_org


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_leak_pinned_org_across_contexts(
) -> None:
    """AUTH-17: two concurrent bare-/mcp service-token requests with
    different pinned orgs must each see only their own org inside the
    inner app — request A's pinned org must never leak into B, even
    while both are parked inside the app with their org set."""
    barrier = asyncio.Barrier(2)
    inner_a = _BarrierInnerApp(barrier)
    inner_b = _BarrierInnerApp(barrier)

    observed_a, observed_b = await asyncio.gather(
        run_one_request_in_own_context(pinned_org="org-a", inner=inner_a),
        run_one_request_in_own_context(pinned_org="org-b", inner=inner_b),
    )

    # Each request observed exactly its own pinned org, before and after
    # the rendezvous — no cross-context bleed.
    assert observed_a == "org-a"
    assert observed_b == "org-b"

    # And the ambient (test-task) context is untouched: the middleware's
    # set/reset is fully contained in each request's copied context, so
    # neither pinned org bled out into the surrounding context. It still
    # reads the module-level ``DEFAULT_ORG_ID`` default.
    ambient = current_org_id.get()
    assert ambient not in {"org-a", "org-b"}
    assert ambient == DEFAULT_ORG_ID


@pytest.mark.asyncio
async def test_concurrent_requests_repeated_no_org_bleed() -> None:
    """AUTH-17 (stress): many interleaved pairs, each pinned to a
    distinct org, all confirm they observed their own org. Repetition
    raises the odds of catching a scheduling-order-dependent leak."""

    async def one_pair(i: int) -> tuple[str | None, str | None]:
        barrier = asyncio.Barrier(2)
        inner_x = _BarrierInnerApp(barrier)
        inner_y = _BarrierInnerApp(barrier)
        return await asyncio.gather(  # type: ignore[return-value]
            run_one_request_in_own_context(
                pinned_org=f"org-{i}-x", inner=inner_x,
            ),
            run_one_request_in_own_context(
                pinned_org=f"org-{i}-y", inner=inner_y,
            ),
        )

    pairs = await asyncio.gather(*[one_pair(i) for i in range(25)])
    for i, (obs_x, obs_y) in enumerate(pairs):
        assert obs_x == f"org-{i}-x"
        assert obs_y == f"org-{i}-y"
