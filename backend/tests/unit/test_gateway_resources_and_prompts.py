"""Gateway controller — list_resources / read_resource / list_prompts /
get_prompt across single-org and multi-org code paths.

Mirrors ``test_multi_org_gateway.py``: build a runtime manager with
seeded registries + stub routers, drive the SDK ``request_handlers``
dict directly so we exercise the registered ``@server.*`` decorator
branches.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
from pydantic import AnyUrl

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    DiscoveredResourceTemplate,
    HttpTransportConfig,
    PromptArgument,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.domain.ports.organization_repository import (
    Membership,
    Organization,
)
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.uri_wrapping import (
    unwrap_resource_uri,
    wrap_resource_uri,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
    current_org_id,
)


# ─── Helpers ──────────────────────────────────────────────────────────────


class InMemoryOrgRepo:
    """Same shape as in test_multi_org_gateway.py — tiny enough to repeat
    here rather than coupling the two test files."""

    def __init__(
        self, *, orgs: list[Organization], memberships: list[Membership],
    ) -> None:
        self._orgs = {o.id: o for o in orgs}
        self._slugs = {o.slug: o for o in orgs}
        self._memberships = memberships

    async def get_organization(self, org_id: str) -> Organization | None:
        return self._orgs.get(org_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        return self._slugs.get(slug)

    async def get_memberships_for_email(
        self, email: str,
    ) -> list[Membership]:
        return [m for m in self._memberships if m.email == email]

    async def list_organizations(self) -> list[Organization]:
        return list(self._orgs.values())


def make_org(org_id: str, slug: str, display_name: str) -> Organization:
    return Organization(
        id=org_id, slug=slug, display_name=display_name,
        created_at=datetime.now(UTC),
        created_by_email="creator@test.com",
    )


def make_membership(
    org_id: str, email: str, role: str = "default",
) -> Membership:
    return Membership(
        org_id=org_id, email=email, role=role,
        created_at=datetime.now(UTC),
    )


def make_upstream_def(upstream_id: str) -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id, display_name=upstream_id.title(),
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=f"http://upstream.invalid/{upstream_id}"),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


def make_runtime_for_org(
    org_id: str,
    upstream_specs: list[tuple[str, list[str], list[str]]],
) -> OrgRuntime:
    """Build a runtime with registries seeded directly.

    Each ``(upstream_id, [resource_uris], [prompt_names])`` tuple
    populates ``DiscoveredResource`` / ``DiscoveredPrompt`` records on
    the registry so the gateway emits them at list time.
    """
    upstreams = [make_upstream_def(uid) for uid, _, _ in upstream_specs]
    cm = UpstreamClientManager(upstreams)
    registry = ToolRegistry(upstreams, cm)
    for uid, uris, prompts in upstream_specs:
        registry._resources.extend([  # pyright: ignore[reportPrivateUsage]
            DiscoveredResource(
                upstream_id=uid, original_uri=u, name=u.split("/")[-1] or u,
                description=f"resource on {uid}",
                mime_type="text/plain",
            )
            for u in uris
        ])
        registry._resource_templates.extend([  # pyright: ignore[reportPrivateUsage]
            DiscoveredResourceTemplate(
                upstream_id=uid,
                original_uri_template=f"{uid}://things/{{id}}",
                name="thing", description="a template",
            )
        ])
        registry._prompts.extend([  # pyright: ignore[reportPrivateUsage]
            DiscoveredPrompt(
                upstream_id=uid, original_name=p,
                prefixed_name=f"{uid}__{p}",
                description=f"prompt {p}",
                arguments=[PromptArgument(name="name", required=True)],
            )
            for p in prompts
        ])
    return OrgRuntime(
        org_id=org_id,
        policy_engine=PolicyEngine(SettingsConfig()),
        tool_registry=registry,
        client_manager=cm,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=upstreams,
    )


def make_runtime_manager_with(
    runtimes: dict[str, OrgRuntime],
    *,
    slugs: dict[str, str] | None = None,
) -> OrgRuntimeManager:
    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    for org_id, rt in runtimes.items():
        manager._runtimes[org_id] = rt  # pyright: ignore[reportPrivateUsage]
        manager._startup_status[org_id] = MagicMock(  # pyright: ignore[reportPrivateUsage]
            ready=True, total=0, connected=set(), failed=set(),
        )
    if slugs:
        for org_id, slug in slugs.items():
            manager.register_slug(org_id, slug)
    return manager


def auth_alice() -> Any:
    """Set request-scoped auth context for ``alice@test.com``."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    auth_user = AuthenticatedUser(
        AccessToken(
            token="fake", client_id="alice@test.com", scopes=[],
            expires_at=int(time.time()) + 3600,
        )
    )
    return auth_context_var.set(auth_user)


def reset_auth(token: Any) -> None:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    auth_context_var.reset(token)


# ─── list_resources ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resources_single_org_prefixes_with_upstream_only() -> None:
    runtime = make_runtime_for_org(
        DEFAULT_ORG_ID,
        [("notion", ["test://hello"], [])],
    )
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime},
        slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)

    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[mcp_types.ListResourcesRequest]
        result = await handler(
            mcp_types.ListResourcesRequest(method="resources/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListResourcesResult, result.root)
    assert len(list_result.resources) == 1
    r = list_result.resources[0]
    # Single-org name prefix is just ``{upstream}__``
    assert r.name == "notion__hello"
    # URI is wrapped with the org slug.
    decoded = unwrap_resource_uri(str(r.uri))
    assert decoded.org_slug == "default"
    assert decoded.upstream_id == "notion"
    assert decoded.original_uri == "test://hello"


@pytest.mark.asyncio
async def test_list_resources_single_org_prefixes_title_with_display_name() -> None:
    # ``display_name`` is folded into the human ``title`` so same-named
    # resources from different upstreams stay distinguishable. The
    # upstream id "notion" → display_name "Notion" (see make_upstream_def).
    runtime = make_runtime_for_org(DEFAULT_ORG_ID, [("notion", [], [])])
    runtime.tool_registry._resources.append(  # pyright: ignore[reportPrivateUsage]
        DiscoveredResource(
            upstream_id="notion", original_uri="test://projects",
            name="projects", title="Get Projects",
            description="d", mime_type="text/plain",
        )
    )
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime}, slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)
    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[mcp_types.ListResourcesRequest]
        result = await handler(
            mcp_types.ListResourcesRequest(method="resources/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListResourcesResult, result.root)
    r = next(x for x in list_result.resources if x.name == "notion__projects")
    assert r.title == "Notion: Get Projects"


@pytest.mark.asyncio
async def test_list_resource_templates_single_org_prefixes_title() -> None:
    runtime = make_runtime_for_org(DEFAULT_ORG_ID, [("notion", [], [])])
    runtime.tool_registry._resource_templates.append(  # pyright: ignore[reportPrivateUsage]
        DiscoveredResourceTemplate(
            upstream_id="notion",
            original_uri_template="notion://reports/{id}",
            name="report", title="Get Report", description="d",
        )
    )
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime}, slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)
    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[
            mcp_types.ListResourceTemplatesRequest
        ]
        result = await handler(
            mcp_types.ListResourceTemplatesRequest(
                method="resources/templates/list",
            ),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListResourceTemplatesResult, result.root)
    t = next(
        x for x in list_result.resourceTemplates if x.name == "notion__report"
    )
    assert t.title == "Notion: Get Report"


@pytest.mark.asyncio
async def test_list_prompts_single_org_prefixes_title_with_display_name() -> None:
    runtime = make_runtime_for_org(DEFAULT_ORG_ID, [("notion", [], [])])
    runtime.tool_registry._prompts.append(  # pyright: ignore[reportPrivateUsage]
        DiscoveredPrompt(
            upstream_id="notion", original_name="summarize",
            prefixed_name="notion__summarize", title="Summarize",
            description="d",
        )
    )
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime}, slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)
    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[mcp_types.ListPromptsRequest]
        result = await handler(
            mcp_types.ListPromptsRequest(method="prompts/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListPromptsResult, result.root)
    p = next(x for x in list_result.prompts if x.name == "notion__summarize")
    assert p.title == "Notion: Summarize"


@pytest.mark.asyncio
async def test_list_resources_multi_org_aggregates_with_three_part_prefix() -> None:
    org_a_runtime = make_runtime_for_org(
        "acme-id", [("notion", ["test://hello"], [])]
    )
    org_b_runtime = make_runtime_for_org(
        "beta-id", [("slack", ["slack://channel/general"], [])]
    )
    rm = make_runtime_manager_with(
        {"acme-id": org_a_runtime, "beta-id": org_b_runtime},
        slugs={"acme-id": "acme", "beta-id": "beta"},
    )

    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
            make_membership("beta-id", "alice@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={"default": RoleDefinition(is_default=True, settings=RoleSettings())},
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    from mcpolis.domain.services.org_service import OrgService
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]

    server = create_mcp_server(rm, org_service=org_service)

    auth_token = auth_alice()
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.ListResourcesRequest]
        result = await handler(
            mcp_types.ListResourcesRequest(method="resources/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    names = sorted(
        r.name for r in cast(
            mcp_types.ListResourcesResult, result.root,
        ).resources
    )
    # Multi-org name prefix is ``{slug}__{upstream}__{name}``.
    assert names == ["acme__notion__hello", "beta__slack__general"]


@pytest.mark.asyncio
async def test_list_resources_multi_org_returns_empty_for_user_in_no_orgs() -> None:
    rm = make_runtime_manager_with({})
    org_repo = InMemoryOrgRepo(orgs=[], memberships=[])
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig())
    from mcpolis.domain.services.org_service import OrgService
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]

    server = create_mcp_server(rm, org_service=org_service)

    auth_token = auth_alice()
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.ListResourcesRequest]
        result = await handler(
            mcp_types.ListResourcesRequest(method="resources/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListResourcesResult, result.root)
    assert list_result.resources == []


# ─── read_resource ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_resource_single_org_unwraps_and_dispatches_to_router() -> None:
    runtime = make_runtime_for_org(
        DEFAULT_ORG_ID, [("notion", ["test://hello"], [])],
    )
    captured: dict[str, Any] = {}

    async def stub_read_resource(
        *, org_id: str, upstream_id: str, original_uri: str,
        user_id: str, session_id: str | None,
    ) -> mcp_types.ReadResourceResult:
        captured["org_id"] = org_id
        captured["upstream_id"] = upstream_id
        captured["original_uri"] = original_uri
        return mcp_types.ReadResourceResult(
            contents=[
                mcp_types.TextResourceContents(
                    uri=AnyUrl("test://hello"),
                    mimeType="text/plain",
                    text="Hello, world!",
                ),
            ],
        )

    runtime.tool_router.read_resource = stub_read_resource  # type: ignore[assignment]
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime}, slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)

    wrapped = wrap_resource_uri(
        org_slug="default", upstream_id="notion",
        original_uri="test://hello",
    )

    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[mcp_types.ReadResourceRequest]
        request = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(uri=AnyUrl(wrapped)),
        )
        result = await handler(request)
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    read_result = cast(mcp_types.ReadResourceResult, result.root)
    text = read_result.contents[0]
    assert isinstance(text, mcp_types.TextResourceContents)
    assert text.text == "Hello, world!"

    assert captured["org_id"] == DEFAULT_ORG_ID
    assert captured["upstream_id"] == "notion"
    assert captured["original_uri"] == "test://hello"


@pytest.mark.asyncio
async def test_read_resource_multi_org_rejects_non_member_slug() -> None:
    """A wrapped URI naming an org the user isn't a member of must fail
    with an access-denied message — never dispatch to the router."""
    org_a_runtime = make_runtime_for_org(
        "acme-id", [("notion", ["test://hello"], [])],
    )
    org_b_runtime = make_runtime_for_org(
        "beta-id", [("slack", ["slack://channel/general"], [])],
    )
    org_b_runtime.tool_router.read_resource = AsyncMock()  # would fail the test if called

    rm = make_runtime_manager_with(
        {"acme-id": org_a_runtime, "beta-id": org_b_runtime},
        slugs={"acme-id": "acme", "beta-id": "beta"},
    )

    # alice is only in acme, not beta.
    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={"default": RoleDefinition(is_default=True, settings=RoleSettings())},
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    from mcpolis.domain.services.org_service import OrgService
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]

    server = create_mcp_server(rm, org_service=org_service)

    wrapped = wrap_resource_uri(
        org_slug="beta", upstream_id="slack",
        original_uri="slack://channel/general",
    )

    auth_token = auth_alice()
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.ReadResourceRequest]
        request = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(uri=AnyUrl(wrapped)),
        )
        result = await handler(request)
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)

    org_b_runtime.tool_router.read_resource.assert_not_awaited()
    text = cast(mcp_types.ReadResourceResult, result.root).contents[0]
    assert isinstance(text, mcp_types.TextResourceContents)
    assert "not a member" in text.text.lower()


# ─── list_prompts / get_prompt ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_prompts_single_org_uses_two_part_prefix() -> None:
    runtime = make_runtime_for_org(
        DEFAULT_ORG_ID, [("notion", [], ["greet"])],
    )
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime}, slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)

    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[mcp_types.ListPromptsRequest]
        result = await handler(
            mcp_types.ListPromptsRequest(method="prompts/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListPromptsResult, result.root)
    assert [p.name for p in list_result.prompts] == ["notion__greet"]


@pytest.mark.asyncio
async def test_list_prompts_multi_org_uses_three_part_prefix() -> None:
    org_a_runtime = make_runtime_for_org(
        "acme-id", [("notion", [], ["greet"])],
    )
    rm = make_runtime_manager_with(
        {"acme-id": org_a_runtime}, slugs={"acme-id": "acme"},
    )
    org_repo = InMemoryOrgRepo(
        orgs=[make_org("acme-id", "acme", "Acme")],
        memberships=[make_membership("acme-id", "alice@test.com")],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={"default": RoleDefinition(is_default=True, settings=RoleSettings())},
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    from mcpolis.domain.services.org_service import OrgService
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]
    server = create_mcp_server(rm, org_service=org_service)

    auth_token = auth_alice()
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.ListPromptsRequest]
        result = await handler(
            mcp_types.ListPromptsRequest(method="prompts/list"),
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(mcp_types.ListPromptsResult, result.root)
    assert [p.name for p in list_result.prompts] == ["acme__notion__greet"]


@pytest.mark.asyncio
async def test_get_prompt_multi_org_routes_by_slug() -> None:
    runtime = make_runtime_for_org(
        "acme-id", [("notion", [], ["greet"])],
    )
    captured: dict[str, Any] = {}

    async def stub_get_prompt(
        *, org_id: str, upstream_id: str, original_name: str,
        arguments: dict[str, str] | None,
        user_id: str, session_id: str | None,
    ) -> mcp_types.GetPromptResult:
        captured["org_id"] = org_id
        captured["upstream_id"] = upstream_id
        captured["original_name"] = original_name
        captured["arguments"] = arguments
        return mcp_types.GetPromptResult(
            description=None,
            messages=[
                mcp_types.PromptMessage(
                    role="user",
                    content=mcp_types.TextContent(type="text", text="ok"),
                ),
            ],
        )

    runtime.tool_router.get_prompt = stub_get_prompt  # type: ignore[assignment]
    rm = make_runtime_manager_with(
        {"acme-id": runtime}, slugs={"acme-id": "acme"},
    )
    org_repo = InMemoryOrgRepo(
        orgs=[make_org("acme-id", "acme", "Acme")],
        memberships=[make_membership("acme-id", "alice@test.com")],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={"default": RoleDefinition(is_default=True, settings=RoleSettings())},
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    from mcpolis.domain.services.org_service import OrgService
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]
    server = create_mcp_server(rm, org_service=org_service)

    auth_token = auth_alice()
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.GetPromptRequest]
        request = mcp_types.GetPromptRequest(
            method="prompts/get",
            params=mcp_types.GetPromptRequestParams(
                name="acme__notion__greet",
                arguments={"name": "world"},
            ),
        )
        await handler(request)
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)

    assert captured["org_id"] == "acme-id"
    assert captured["upstream_id"] == "notion"
    assert captured["original_name"] == "greet"
    assert captured["arguments"] == {"name": "world"}


@pytest.mark.asyncio
async def test_get_prompt_multi_org_rejects_non_member_slug() -> None:
    """Mirrors ``read_resource`` — slug a user can't see must be denied."""
    runtime = make_runtime_for_org(
        "beta-id", [("slack", [], ["greet"])],
    )
    runtime.tool_router.get_prompt = AsyncMock()  # would fail if invoked
    rm = make_runtime_manager_with(
        {"beta-id": runtime}, slugs={"beta-id": "beta"},
    )

    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[make_membership("acme-id", "alice@test.com")],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig())
    from mcpolis.domain.services.org_service import OrgService
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]
    server = create_mcp_server(rm, org_service=org_service)

    auth_token = auth_alice()
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.GetPromptRequest]
        request = mcp_types.GetPromptRequest(
            method="prompts/get",
            params=mcp_types.GetPromptRequestParams(
                name="beta__slack__greet",
                arguments=None,
            ),
        )
        result = await handler(request)
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)

    runtime.tool_router.get_prompt.assert_not_awaited()
    msg = cast(mcp_types.GetPromptResult, result.root).messages[0]
    assert isinstance(msg.content, mcp_types.TextContent)
    assert "not a member" in msg.content.text.lower()


# ─── list_resource_templates ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resource_templates_single_org_round_trips() -> None:
    runtime = make_runtime_for_org(
        DEFAULT_ORG_ID, [("notion", [], [])]
    )
    rm = make_runtime_manager_with(
        {DEFAULT_ORG_ID: runtime}, slugs={DEFAULT_ORG_ID: "default"},
    )
    server = create_mcp_server(rm)

    auth_token = auth_alice()
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        handler = server.request_handlers[
            mcp_types.ListResourceTemplatesRequest
        ]
        result = await handler(
            mcp_types.ListResourceTemplatesRequest(
                method="resources/templates/list",
            )
        )
    finally:
        current_org_id.reset(org_token)
        reset_auth(auth_token)
    list_result = cast(
        mcp_types.ListResourceTemplatesResult, result.root,
    )
    assert len(list_result.resourceTemplates) == 1
    t = list_result.resourceTemplates[0]
    decoded = unwrap_resource_uri(t.uriTemplate)
    assert decoded.is_template is True
    assert decoded.org_slug == "default"
    assert decoded.upstream_id == "notion"
