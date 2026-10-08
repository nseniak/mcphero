"""Tool-call rate limits inside the gateway and the Admin MCP.

The gateway charges every ``tools/call`` to its caller, and to its org
when the org's policy allows the call; the Admin MCP charges every call
to the calling user. A refusal is an ``isError`` tool result whose text
names the wait, and the refused call never reaches the upstream. A
refused ``resources/read`` or ``prompts/get`` is charged like a refused
tool call: to the caller only.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel.server import Server
from pydantic import AnyUrl

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.domain.model.service_token import (
    ServiceAccessToken,
    service_identity,
)
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.domain.ports.rate_limiter import RateLimitBucket, RateLimitResult
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.plan_policy import FREE
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.rate_limit_service import (
    RateLimitService,
    RequestRateLimits,
)
from mcpolis.domain.services.uri_wrapping import wrap_resource_uri
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
    current_org_id,
    current_user_id,
)
from tests.unit._loopback_mcp import free_ports, mcp_session_call, wait_for_health
from tests.unit.factories import (
    make_config_users_accepted,
    make_full_access_config,
    make_runtime_manager,
)
from tests.unit.test_gateway_controller import make_gateway_components
from tests.unit.test_multi_org_gateway import (
    InMemoryOrgRepo,
    make_membership,
    make_org,
    make_runtime_manager_with_orgs,
)


class RecordingRateLimiter:
    """In-process limiter that remembers which buckets were charged."""

    def __init__(self) -> None:
        self._inner = InProcessRateLimiter()
        self.charged_keys: list[str] = []

    async def check(self, *buckets: RateLimitBucket) -> RateLimitResult:
        result = await self._inner.check(*buckets)
        if result.allowed:
            self.charged_keys += [b.key for b in buckets]
        return result

    async def close(self) -> None:
        await self._inner.close()


def make_rate_limits(
    limiter: RecordingRateLimiter | None = None,
    *,
    limits: RequestRateLimits | None = None,
) -> RateLimitService:
    async def free_plan(_org_id: str) -> PlanName:
        return PlanName.free

    return RateLimitService(
        limiter or RecordingRateLimiter(),
        limits or RequestRateLimits(),
        plan_for_org=free_plan,
    )


def make_ok_router() -> MagicMock:
    router = MagicMock()
    router.route_call = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="done")],
        ),
    )
    router.audit_denied = AsyncMock()
    return router


@contextmanager
def signed_in_as(
    client_id: str, access_token: AccessToken | None = None,
) -> Iterator[None]:
    token = auth_context_var.set(
        AuthenticatedUser(
            access_token or AccessToken(
                token="fake",
                client_id=client_id,
                scopes=[],
                expires_at=int(time.time()) + 3600,
            ),
        ),
    )
    try:
        yield
    finally:
        auth_context_var.reset(token)


async def call_tool(server: Server[Any, Any], name: str) -> mcp_types.CallToolResult:
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments={}),
    )
    result = cast(Any, await handler(request))
    return cast(mcp_types.CallToolResult, result.root)


def text_of(result: mcp_types.CallToolResult) -> str:
    return cast(mcp_types.TextContent, result.content[0]).text


def make_single_org_gateway(
    tmp_path: Path, rate_limits: RateLimitService, users: list[str],
) -> tuple[Server[Any, Any], MagicMock]:
    registry, _router, _upstreams = make_gateway_components(tmp_path)
    router = make_ok_router()
    rm = make_runtime_manager(
        PolicyEngine(make_full_access_config(["github", "slack"], users)),
        tool_registry=registry,
        tool_router=router,
    )
    return create_mcp_server(rm, rate_limits=rate_limits), router


# ── Gateway, single org ───────────────────────────────────────────────


async def test_gateway_refuses_calls_over_the_caller_limit(tmp_path: Path) -> None:
    server, router = make_single_org_gateway(
        tmp_path, make_rate_limits(), ["alice@example.com"],
    )
    with signed_in_as("alice@example.com"):
        admitted = [
            await call_tool(server, "github__create_issue")
            for _ in range(FREE.tool_calls_per_min_per_caller)
        ]
        refused = await call_tool(server, "github__create_issue")

    assert all(not r.isError for r in admitted)
    assert refused.isError
    assert text_of(refused).startswith(
        "Rate limit reached: you have made too many tool calls",
    )
    assert "Try again in 60 seconds." in text_of(refused)
    # The refused call never reached the upstream.
    assert router.route_call.await_count == FREE.tool_calls_per_min_per_caller


async def test_gateway_charges_each_caller_separately(tmp_path: Path) -> None:
    server, _router = make_single_org_gateway(
        tmp_path, make_rate_limits(), ["alice@example.com", "bob@example.com"],
    )
    with signed_in_as("alice@example.com"):
        for _ in range(FREE.tool_calls_per_min_per_caller + 5):
            await call_tool(server, "github__create_issue")
    with signed_in_as("bob@example.com"):
        bob = await call_tool(server, "github__create_issue")

    assert not bob.isError


async def test_gateway_charges_a_service_token_under_its_own_identity(
    tmp_path: Path,
) -> None:
    limiter = RecordingRateLimiter()
    server, _router = make_single_org_gateway(
        tmp_path, make_rate_limits(limiter), ["alice@example.com"],
    )
    bot = service_identity("nightly-bot")
    service_token = ServiceAccessToken(
        token="svct_fake", client_id=bot, scopes=[],
        role_name="default", org_id=DEFAULT_ORG_ID,
    )
    with signed_in_as(bot, service_token):
        await call_tool(server, "github__create_issue")

    assert limiter.charged_keys == [
        f"tool_call:caller:{DEFAULT_ORG_ID}:svc:nightly-bot",
        f"tool_call:org:{DEFAULT_ORG_ID}",
    ]


async def test_outsider_on_the_slug_gateway_cannot_spend_the_orgs_quota(
    tmp_path: Path,
) -> None:
    """Any signed-in account can reach ``/mcp/{slug}``: membership there
    is enforced by policy. An outsider's calls are denied by policy and
    charged to the outsider only, never to the org's shared quota, and
    once over its own limit they stop writing audit rows too."""
    limiter = RecordingRateLimiter()
    server, router = make_single_org_gateway(
        tmp_path, make_rate_limits(limiter), ["alice@example.com"],
    )
    with signed_in_as("mallory@example.com"):
        results = [
            await call_tool(server, "github__create_issue")
            for _ in range(FREE.tool_calls_per_min_per_caller + 20)
        ]
    with signed_in_as("alice@example.com"):
        alice = await call_tool(server, "github__create_issue")

    texts = [text_of(r) for r in results]
    limit = FREE.tool_calls_per_min_per_caller
    assert all(t.startswith("Access denied") for t in texts[:limit])
    assert all(t.startswith("Rate limit reached") for t in texts[limit:])
    org_key = f"tool_call:org:{DEFAULT_ORG_ID}"
    assert limiter.charged_keys.count(org_key) == 1  # alice's call only
    assert not alice.isError
    assert router.audit_denied.await_count == FREE.tool_calls_per_min_per_caller
    router.route_call.assert_awaited_once()


async def test_outsider_refusal_reveals_nothing_about_the_orgs_plan(
    tmp_path: Path,
) -> None:
    server, _router = make_single_org_gateway(
        tmp_path, make_rate_limits(), ["alice@example.com"],
    )
    with signed_in_as("mallory@example.com"):
        for _ in range(FREE.tool_calls_per_min_per_caller):
            await call_tool(server, "github__create_issue")
        refused = await call_tool(server, "github__create_issue")

    assert text_of(refused).startswith("Rate limit reached")
    assert "plan" not in text_of(refused).lower()


async def test_unknown_bare_tool_names_are_bounded(tmp_path: Path) -> None:
    limiter = RecordingRateLimiter()
    server, router = make_single_org_gateway(
        tmp_path, make_rate_limits(limiter), ["alice@example.com"],
    )
    with signed_in_as("alice@example.com"):
        texts = [
            text_of(await call_tool(server, "no-such-tool"))
            for _ in range(FREE.tool_calls_per_min_per_caller + 1)
        ]

    assert all(t.startswith("Unknown tool") for t in texts[:-1])
    assert texts[-1].startswith("Rate limit reached")
    assert set(limiter.charged_keys) == {"tool_call:denied:alice@example.com"}
    router.route_call.assert_not_awaited()


async def test_gateway_without_rate_limits_never_refuses(tmp_path: Path) -> None:
    registry, _router, _upstreams = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(make_full_access_config(["github"], ["anonymous"])),
        tool_registry=registry,
        tool_router=make_ok_router(),
    )
    server = create_mcp_server(rm)

    results = [
        await call_tool(server, "github__create_issue")
        for _ in range(FREE.tool_calls_per_min_per_caller + 5)
    ]

    assert all(not r.isError for r in results)


# ── Gateway, multi-org ────────────────────────────────────────────────


def make_multi_org_gateway(
    rate_limits: RateLimitService,
) -> tuple[Server[Any, Any], OrgService]:
    """alice belongs to acme only; bob belongs to beta."""
    org_repo = InMemoryOrgRepo(
        orgs=[make_org("acme-id", "acme", "Acme"), make_org("beta-id", "beta", "Beta")],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
            make_membership("beta-id", "bob@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={"default": RoleDefinition(is_default=True, settings=RoleSettings())},
        users={
            "alice@test.com": UserDefinition(role="default"),
            "bob@test.com": UserDefinition(role="default"),
        },
    ))
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]
    manager = make_runtime_manager_with_orgs(
        [("acme-id", ["gmail"]), ("beta-id", ["slack"])],
        user_emails=["alice@test.com", "bob@test.com"],
    )
    for runtime_org in ("acme-id", "beta-id"):
        manager._runtimes[runtime_org].tool_router = make_ok_router()  # pyright: ignore[reportPrivateUsage]
    server = create_mcp_server(manager, org_service=org_service, rate_limits=rate_limits)
    return server, org_service


async def test_multi_org_call_is_charged_to_the_named_org() -> None:
    limiter = RecordingRateLimiter()
    server, _org_service = make_multi_org_gateway(make_rate_limits(limiter))
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        with signed_in_as("alice@test.com"):
            result = await call_tool(server, "acme__gmail__do_thing")
    finally:
        current_org_id.reset(org_token)

    assert not result.isError
    assert limiter.charged_keys == [
        "tool_call:caller:acme-id:alice@test.com",
        "tool_call:org:acme-id",
    ]


async def test_non_member_cannot_spend_another_orgs_quota() -> None:
    """A client can type any org slug. Calls naming an org the caller
    doesn't belong to are charged to the caller's refused-call bucket
    only, never to the named org."""
    limiter = RecordingRateLimiter()
    server, _org_service = make_multi_org_gateway(make_rate_limits(limiter))
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        with signed_in_as("alice@test.com"):
            for _ in range(FREE.tool_calls_per_min_per_org + 10):
                await call_tool(server, "beta__slack__do_thing")
        with signed_in_as("bob@test.com"):
            bob = await call_tool(server, "beta__slack__do_thing")
    finally:
        current_org_id.reset(org_token)

    assert not bob.isError
    assert limiter.charged_keys.count("tool_call:org:beta-id") == 1  # bob's call
    assert set(limiter.charged_keys) == {
        "tool_call:denied:alice@test.com",
        "tool_call:caller:beta-id:bob@test.com",
        "tool_call:org:beta-id",
    }


async def test_calls_naming_a_foreign_org_are_bounded() -> None:
    """Each such call costs a membership lookup; without a bound a
    client could loop on them forever."""
    limiter = RecordingRateLimiter()
    server, _org_service = make_multi_org_gateway(make_rate_limits(limiter))
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        with signed_in_as("alice@test.com"):
            texts = [
                text_of(await call_tool(server, "beta__slack__do_thing"))
                for _ in range(FREE.tool_calls_per_min_per_caller + 1)
            ]
    finally:
        current_org_id.reset(org_token)

    assert all("not a member" in t for t in texts[:-1])
    assert texts[-1].startswith("Rate limit reached")
    assert set(limiter.charged_keys) == {"tool_call:denied:alice@test.com"}


# ── Gateway, refused resource reads and prompts ───────────────────────


async def read_resource(server: Server[Any, Any], uri: str) -> str:
    """The text a ``resources/read`` answers with."""
    handler = server.request_handlers[mcp_types.ReadResourceRequest]
    request = mcp_types.ReadResourceRequest(
        method="resources/read",
        params=mcp_types.ReadResourceRequestParams(uri=AnyUrl(uri)),
    )
    result = cast(Any, await handler(request))
    content = cast(mcp_types.ReadResourceResult, result.root).contents[0]
    assert isinstance(content, mcp_types.TextResourceContents)
    return content.text


async def get_prompt(server: Server[Any, Any], name: str) -> str:
    """The text a ``prompts/get`` answers with."""
    handler = server.request_handlers[mcp_types.GetPromptRequest]
    request = mcp_types.GetPromptRequest(
        method="prompts/get",
        params=mcp_types.GetPromptRequestParams(name=name, arguments={}),
    )
    result = cast(Any, await handler(request))
    message = cast(mcp_types.GetPromptResult, result.root).messages[0]
    assert isinstance(message.content, mcp_types.TextContent)
    return message.content.text


RequestKind = Literal["read", "prompt"]


def sender_of(kind: RequestKind) -> Callable[[Server[Any, Any], str], Awaitable[str]]:
    return read_resource if kind == "read" else get_prompt


@contextmanager
def in_org_context(org_id: str) -> Iterator[None]:
    """The org the gateway URL names (``MULTI_ORG_SENTINEL`` for bare
    ``/mcp``), as ``OrgContextMiddleware`` sets it."""
    token = current_org_id.set(org_id)
    try:
        yield
    finally:
        current_org_id.reset(token)


async def test_outsider_resource_reads_cannot_flood_the_orgs_audit_log(
    tmp_path: Path,
) -> None:
    """A refused read writes a ``denied`` row into the org's log, and the
    URI it names, the outsider's own text, lands in that row. Charged
    like a refused tool call, the outsider gets the rate-limit refusal
    once over its refused-call limit, and no row is written for it."""
    limiter = RecordingRateLimiter()
    server, router = make_single_org_gateway(
        tmp_path, make_rate_limits(limiter), ["alice@example.com"],
    )
    limit = FREE.tool_calls_per_min_per_caller
    with signed_in_as("mallory@example.com"):
        texts = [
            await read_resource(server, wrap_resource_uri(
                org_slug="victim", upstream_id="github",
                original_uri=f"file:///junk/{i}/" + "x" * 2000,
            ))
            for i in range(limit + 50)
        ]

    assert all(t.startswith("Access denied") for t in texts[:limit])
    assert all(t.startswith("Rate limit reached") for t in texts[limit:])
    assert router.audit_denied.await_count == limit
    assert set(limiter.charged_keys) == {"tool_call:denied:mallory@example.com"}


async def test_outsider_prompt_gets_cannot_flood_the_orgs_audit_log(
    tmp_path: Path,
) -> None:
    limiter = RecordingRateLimiter()
    server, router = make_single_org_gateway(
        tmp_path, make_rate_limits(limiter), ["alice@example.com"],
    )
    limit = FREE.tool_calls_per_min_per_caller
    with signed_in_as("mallory@example.com"):
        texts = [
            await get_prompt(server, f"github__{i}" + "y" * 2000)
            for i in range(limit + 50)
        ]

    assert all(t.startswith("Access denied") for t in texts[:limit])
    assert all(t.startswith("Rate limit reached") for t in texts[limit:])
    assert router.audit_denied.await_count == limit
    assert set(limiter.charged_keys) == {"tool_call:denied:mallory@example.com"}


def assert_charged_to_the_caller_only(
    texts: list[str], limiter: RecordingRateLimiter, caller: str,
) -> None:
    """``texts`` answered one more refused request than the refused-call
    limit: only the last one is the rate-limit refusal, and only the
    caller's refused-call bucket was ever charged."""
    limit = FREE.tool_calls_per_min_per_caller
    assert not any(t.startswith("Rate limit reached") for t in texts[:limit])
    assert texts[limit] == (
        "Rate limit reached: you have made too many requests that were "
        "refused in the last minute. Try again in 60 seconds."
    )
    assert set(limiter.charged_keys) == {f"tool_call:denied:{caller}"}


@pytest.mark.parametrize(("kind", "target"), [
    pytest.param(
        "read",
        wrap_resource_uri(org_slug="default", upstream_id="nope", original_uri="a://b"),
        id="read-of-an-mcp-the-org-lacks",
    ),
    pytest.param("read", "file:///not-wrapped", id="read-of-an-unknown-bare-uri"),
    pytest.param("prompt", "nope__greet", id="prompt-of-an-mcp-the-org-lacks"),
    pytest.param("prompt", "greet", id="prompt-without-an-mcp-prefix"),
])
async def test_every_refused_read_or_prompt_on_a_slug_url_is_charged_to_the_caller(
    tmp_path: Path, kind: RequestKind, target: str,
) -> None:
    limiter = RecordingRateLimiter()
    server, _router = make_single_org_gateway(
        tmp_path, make_rate_limits(limiter), ["alice@example.com"],
    )
    send = sender_of(kind)
    with signed_in_as("mallory@example.com"):
        texts = [
            await send(server, target)
            for _ in range(FREE.tool_calls_per_min_per_caller + 1)
        ]

    assert_charged_to_the_caller_only(texts, limiter, "mallory@example.com")


@pytest.mark.parametrize(("kind", "target"), [
    pytest.param(
        "read",
        wrap_resource_uri(org_slug="beta", upstream_id="slack", original_uri="a://b"),
        id="read-in-an-org-the-caller-is-not-in",
    ),
    pytest.param("read", "file:///not-wrapped", id="read-of-an-unknown-bare-uri"),
    pytest.param(
        "read",
        wrap_resource_uri(org_slug="acme", upstream_id="nope", original_uri="a://b"),
        id="read-of-an-mcp-the-org-lacks",
    ),
    pytest.param("prompt", "beta__slack__greet", id="prompt-in-an-org-the-caller-is-not-in"),
    pytest.param("prompt", "greet", id="prompt-without-an-org-prefix"),
    pytest.param("prompt", "acme__nope__greet", id="prompt-of-an-mcp-the-org-lacks"),
])
async def test_every_refused_read_or_prompt_on_the_multi_org_url_is_charged_to_the_caller(
    kind: RequestKind, target: str,
) -> None:
    limiter = RecordingRateLimiter()
    server, _org_service = make_multi_org_gateway(make_rate_limits(limiter))
    send = sender_of(kind)
    with in_org_context(MULTI_ORG_SENTINEL), signed_in_as("alice@test.com"):
        texts = [
            await send(server, target)
            for _ in range(FREE.tool_calls_per_min_per_caller + 1)
        ]

    assert_charged_to_the_caller_only(texts, limiter, "alice@test.com")


# ── Admin MCP ─────────────────────────────────────────────────────────


async def call_admin_tool(server: Any, name: str, user: str) -> mcp_types.CallToolResult:
    """Through the low-level ``tools/call`` handler — the path a real
    client takes, and the one the rate limit wraps."""
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    user_token = current_user_id.set(user)
    try:
        return await call_tool(server._mcp_server, name)
    finally:
        current_org_id.reset(org_token)
        current_user_id.reset(user_token)


def make_admin_server(rate_limits: RateLimitService | None) -> Any:
    rm = make_runtime_manager(
        PolicyEngine(make_full_access_config([], ["admin@example.com"])),
    )
    return create_admin_mcp_server(
        runtime_manager=rm,
        audit_repo=MagicMock(),
        policy_store=MagicMock(),
        template_var_repo=MagicMock(),
        rate_limits=rate_limits,
    )


async def test_admin_mcp_refuses_calls_over_the_user_limit() -> None:
    server = make_admin_server(
        make_rate_limits(limits=RequestRateLimits(admin_mcp_per_min=2)),
    )
    admitted = [
        await call_admin_tool(server, "list_roles", "admin@example.com")
        for _ in range(2)
    ]
    refused = await call_admin_tool(server, "list_roles", "admin@example.com")
    other_admin = await call_admin_tool(server, "list_roles", "other@example.com")

    assert all(not r.isError for r in admitted)
    assert '"name"' in text_of(admitted[0])
    assert refused.isError
    assert text_of(refused) == (
        "Rate limit reached: you have made too many Admin MCP calls in the "
        "last minute. Try again in 60 seconds."
    )
    assert not other_admin.isError


async def test_admin_mcp_without_rate_limits_never_refuses() -> None:
    server = make_admin_server(None)

    results = [
        await call_admin_tool(server, "list_roles", "admin@example.com")
        for _ in range(70)
    ]

    assert all(not r.isError for r in results)


ADMIN_A = "admin-a@example.com"
ADMIN_B = "admin-b@example.com"


def make_admin_app_settings(tmp_path: Path, port: int) -> Settings:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {},
        "roles": {"admin": {"is_admin": True, "settings": {"mcp_access": {"mcps": {}}}}},
        "users": {ADMIN_A: {"role": "admin"}, ADMIN_B: {"role": "admin"}},
    }))
    make_config_users_accepted(tmp_path / "data", config.read_text())
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        host="127.0.0.1",
        port=port,
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        test_mode=True,
        google_client_id="",
        google_client_secret="",
        session_secret="admin-mcp-rate-limit-secret",
        server_url=f"http://127.0.0.1:{port}",
        rate_limit_admin_mcp_per_min=2,
    )


async def call_admin_over_http(port: int, token: str, count: int) -> list[str]:
    async def run(session: ClientSession) -> list[str]:
        texts: list[str] = []
        for _ in range(count):
            result = await session.call_tool("list_roles", {})
            texts.append(cast(mcp_types.TextContent, result.content[0]).text)
        return texts

    return await mcp_session_call(f"http://127.0.0.1:{port}/admin-mcp/", token, run)


async def test_admin_mcp_limit_is_per_admin_over_the_real_transport(
    tmp_path: Path,
) -> None:
    """Through the real HTTP transport with bearer tokens, the path every
    AI client takes. Each admin must have its own bucket: an earlier
    version charged every bearer caller as "anonymous", one bucket for
    every admin of every org."""
    (port,) = free_ports(1)
    app = create_app(make_admin_app_settings(tmp_path, port))
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    try:
        await wait_for_health(f"http://127.0.0.1:{port}/health", label="admin-mcp")
        provider = app.state.mcp_gateway_oauth_provider
        a_texts = await call_admin_over_http(port, await provider.mint_test_token(ADMIN_A), 3)
        b_texts = await call_admin_over_http(port, await provider.mint_test_token(ADMIN_B), 1)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)

    assert [t.startswith("Rate limit reached") for t in a_texts] == [False, False, True]
    assert not b_texts[0].startswith("Rate limit reached")
