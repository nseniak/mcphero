"""Resource reads and prompt gets are refused once a server is switched off.

On the Access page an admin can switch an MCP server off for a role
(``PUT /api/admin/roles/{role}/mcps/{mcp_id}`` with ``enabled: false``).
From then on, a user of that role must not be able to read that server's
resources (``resources/read``) or render its prompts (``prompts/get``)
through the gateway: the request is refused and never reaches the server.
Other servers, and other roles that still have the server on, keep
working. Each refusal also writes one ``denied`` row to the Audit page,
named the way an allowed read or prompt row is named. (Tool calls have
the same rule; it is pinned by ``test_denied_mcp_disabled_writes_audit_entry``
in ``test_gateway_controller.py``.)

The gateway answers reads on two paths, both checked here:

- one org: ``/mcp/{slug}`` in cloud mode, ``/mcp`` in standalone;
- several orgs: the cloud ``/mcp`` that spans every org the user is in.

The handler-level tests drive the gateway's MCP request handlers over a
real policy, a real tool registry (filled from a saved catalog) and a
real config store. The switch-off is made the way the Access page route
makes it: the config store saves the change, then the live policy is
reloaded. A recording stand-in for the upstream servers answers every
read it receives, so the tests can tell a refusal apart from a read that
reached the server. The last test runs the real Access page route on the
standalone app, to show the switch reaches the gateway at once.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import mcp.types as mcp_types
import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel.server import Server
from pydantic import AnyUrl

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import (
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.domain.ports.tool_catalog_repository import ToolCatalogSnapshot
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.uri_wrapping import wrap_resource_uri
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
    current_org_id,
)
from tests.unit._dev_stub_login import login_as
from tests.unit.factories import make_config_users_accepted, make_runtime_manager
from tests.unit.in_memory_tool_catalog_store import make_tool_catalog_store

ORG_SLUG = "default"
ALICE = "alice@example.com"  # role "developer": loses Notion
LENA = "lena@example.com"  # role "lead": keeps Notion
NOTION = "notion"  # the server that gets switched off
GITHUB = "github"  # a server that stays on
RESOURCE_URIS = {NOTION: "notion://pages/roadmap", GITHUB: "github://repos/readme"}
PROMPT_NAMES = {NOTION: "summarize", GITHUB: "triage"}

GatewayServer = Server[Any, Any]


def disabled_reason(upstream_id: str, user: str) -> str:
    """Why the request was refused, as the Audit page shows it."""
    return f"MCP '{upstream_id}' is disabled for user '{user}'."


def disabled_refusal(upstream_id: str, user: str) -> str:
    """The answer a user gets for a server switched off for their role."""
    return f"Access denied: {disabled_reason(upstream_id, user)}"


def resource_row_name(upstream_id: str) -> str:
    """The Audit page's tool column for a read of ``upstream_id``'s
    resource; allowed reads carry the same name."""
    return f"resource:{upstream_id}:{RESOURCE_URIS[upstream_id]}"


def prompt_row_name(upstream_id: str) -> str:
    """The Audit page's tool column for ``upstream_id``'s prompt; allowed
    prompt gets carry the same name."""
    return f"prompt:{upstream_id}:{PROMPT_NAMES[upstream_id]}"


# --- The upstream side ------------------------------------------------


@dataclass(frozen=True)
class DeniedRow:
    """One ``denied`` audit row the gateway asked the router to write."""

    org_id: str
    user_id: str
    upstream_id: str
    tool: str
    reason: str
    policy_rule: str | None


def make_denied_row(user: str, upstream_id: str, tool: str) -> DeniedRow:
    """The row a refusal for a switched-off server must write."""
    return DeniedRow(
        org_id=DEFAULT_ORG_ID, user_id=user, upstream_id=upstream_id,
        tool=tool, reason=disabled_reason(upstream_id, user),
        policy_rule="mcp_disabled",
    )


@dataclass
class RecordingUpstreams:
    """Stands in for the gateway's router: answers every read it
    receives and records who asked for what, as ``(user, server,
    resource URI or prompt name)``, plus every ``denied`` audit row the
    gateway asks it to write."""

    received: list[tuple[str, str, str]] = field(
        default_factory=list[tuple[str, str, str]],
    )
    denied: list[DeniedRow] = field(default_factory=list[DeniedRow])

    async def audit_denied(
        self, org_id: str, *, user_id: str, upstream_id: str, tool: str,
        reason: str, policy_rule: str | None = None,
        session_id: str | None = None,
    ) -> None:
        del session_id
        self.denied.append(DeniedRow(
            org_id=org_id, user_id=user_id, upstream_id=upstream_id,
            tool=tool, reason=reason, policy_rule=policy_rule,
        ))

    async def read_resource(
        self, *, org_id: str, upstream_id: str, original_uri: str,
        user_id: str, session_id: str | None,
    ) -> mcp_types.ReadResourceResult:
        del org_id, session_id
        self.received.append((user_id, upstream_id, original_uri))
        return mcp_types.ReadResourceResult(contents=[
            mcp_types.TextResourceContents(
                uri=AnyUrl(original_uri), mimeType="text/plain",
                text=f"{upstream_id} resource text",
            ),
        ])

    async def get_prompt(
        self, *, org_id: str, upstream_id: str, original_name: str,
        arguments: dict[str, str] | None, user_id: str, session_id: str | None,
    ) -> mcp_types.GetPromptResult:
        del org_id, arguments, session_id
        self.received.append((user_id, upstream_id, original_name))
        return mcp_types.GetPromptResult(messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(
                    type="text", text=f"{upstream_id} prompt text",
                ),
            ),
        ])


# --- Builders -----------------------------------------------------------


def make_upstream(upstream_id: str) -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id, display_name=upstream_id.title(),
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=f"https://{upstream_id}.example.invalid/mcp"),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


def make_catalog(upstream_id: str) -> ToolCatalogSnapshot:
    """What the registry saved for ``upstream_id``: one resource, one prompt."""
    prompt = PROMPT_NAMES[upstream_id]
    return ToolCatalogSnapshot(
        resources=[DiscoveredResource(
            upstream_id=upstream_id, original_uri=RESOURCE_URIS[upstream_id],
            name=f"{upstream_id} page", mime_type="text/plain",
        )],
        prompts=[DiscoveredPrompt(
            upstream_id=upstream_id, original_name=prompt,
            prefixed_name=f"{upstream_id}__{prompt}",
        )],
    )


def make_access_config() -> SettingsConfig:
    """Both roles start with both servers switched on."""
    both_on = McpAccessConfig(mcps={NOTION: True, GITHUB: True})
    return SettingsConfig(
        roles={
            "developer": RoleDefinition(
                is_default=True, settings=RoleSettings(mcp_access=both_on),
            ),
            "lead": RoleDefinition(
                settings=RoleSettings(mcp_access=both_on.model_copy(deep=True)),
            ),
        },
        users={
            ALICE: UserDefinition(role="developer"),
            LENA: UserDefinition(role="lead"),
        },
    )


@dataclass
class Gateway:
    """The gateway for one org, plus what the tests act on and observe."""

    server: GatewayServer
    upstreams: RecordingUpstreams
    config_store: FileConfigStore
    policy_engine: PolicyEngine
    multi_org: bool


async def make_gateway(tmp_path: Path, *, multi_org: bool) -> Gateway:
    upstream_defs = [make_upstream(NOTION), make_upstream(GITHUB)]
    client_manager = UpstreamClientManager(upstream_defs)
    catalog = make_tool_catalog_store()
    for upstream in upstream_defs:
        await catalog.upsert_upstream(DEFAULT_ORG_ID, upstream.id, make_catalog(upstream.id))
    registry = ToolRegistry(
        upstream_defs, client_manager, catalog_repo=catalog, org_id=DEFAULT_ORG_ID,
    )
    await registry.hydrate()

    config_store = FileConfigStore(tmp_path / "config.json")
    await config_store.save(DEFAULT_ORG_ID, make_access_config())
    policy_engine = PolicyEngine(await config_store.load(DEFAULT_ORG_ID))

    upstreams = RecordingUpstreams()
    runtime_manager: OrgRuntimeManager = make_runtime_manager(
        policy_engine,
        tool_registry=registry,
        client_manager=client_manager,
        tool_router=cast(ToolRouter, upstreams),
        upstreams=upstream_defs,
        org_id=DEFAULT_ORG_ID,
    )
    runtime_manager.register_slug(DEFAULT_ORG_ID, ORG_SLUG)

    # Memberships are what the several-orgs gateway uses to find the
    # user's orgs. The file repository holds the one standalone org; a
    # cloud user who belongs to one org takes the same path.
    org_repo = FileOrganizationRepository(tmp_path / "data")
    await org_repo.add_membership(DEFAULT_ORG_ID, ALICE, "developer")
    await org_repo.add_membership(DEFAULT_ORG_ID, LENA, "lead")
    org_service = OrgService(org_repo=org_repo, config_repo=config_store)

    return Gateway(
        server=create_mcp_server(runtime_manager, org_service=org_service),
        upstreams=upstreams,
        config_store=config_store,
        policy_engine=policy_engine,
        multi_org=multi_org,
    )


async def switch_off_for_role(gateway: Gateway, role: str, upstream_id: str) -> None:
    """What the Access page route does: save the change, then reload
    the org's live policy."""
    new_config = await gateway.config_store.set_role_mcp_access_entry(
        DEFAULT_ORG_ID, role, upstream_id, False,
    )
    gateway.policy_engine.reload(new_config)


# --- Talking to the gateway -------------------------------------------


@contextmanager
def signed_in_to_gateway(user: str, org_context: str) -> Iterator[None]:
    """Run gateway handlers as ``user`` on the given org path (an org id,
    or ``MULTI_ORG_SENTINEL`` for the several-orgs gateway), as the
    gateway's auth and org middleware would."""
    auth_token = auth_context_var.set(AuthenticatedUser(AccessToken(
        token="gateway-login", client_id=user, scopes=[],
        expires_at=int(time.time()) + 3600,
    )))
    org_token = current_org_id.set(org_context)
    try:
        yield
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)


async def read_resource(
    server: GatewayServer, org_context: str, user: str, upstream_id: str,
) -> str:
    """``resources/read`` for ``upstream_id``'s resource; returns the text
    the MCP client gets back."""
    wrapped_uri = wrap_resource_uri(
        org_slug=ORG_SLUG, upstream_id=upstream_id,
        original_uri=RESOURCE_URIS[upstream_id],
    )
    with signed_in_to_gateway(user, org_context):
        handler = server.request_handlers[mcp_types.ReadResourceRequest]
        result = await handler(mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(uri=AnyUrl(wrapped_uri)),
        ))
    contents = cast(mcp_types.ReadResourceResult, result.root).contents
    assert len(contents) == 1
    item = contents[0]
    assert isinstance(item, mcp_types.TextResourceContents)
    return item.text


async def get_prompt(
    server: GatewayServer, org_context: str, user: str, upstream_id: str,
) -> str:
    """``prompts/get`` for ``upstream_id``'s prompt; returns the text the
    MCP client gets back. The several-orgs gateway names prompts
    ``{org}__{server}__{prompt}``, the one-org gateway ``{server}__{prompt}``."""
    name = f"{upstream_id}__{PROMPT_NAMES[upstream_id]}"
    if org_context == MULTI_ORG_SENTINEL:
        name = f"{ORG_SLUG}__{name}"
    with signed_in_to_gateway(user, org_context):
        handler = server.request_handlers[mcp_types.GetPromptRequest]
        result = await handler(mcp_types.GetPromptRequest(
            method="prompts/get",
            params=mcp_types.GetPromptRequestParams(name=name, arguments=None),
        ))
    messages = cast(mcp_types.GetPromptResult, result.root).messages
    assert len(messages) == 1
    content = messages[0].content
    assert isinstance(content, mcp_types.TextContent)
    return content.text


def org_context_for(gateway: Gateway) -> str:
    return MULTI_ORG_SENTINEL if gateway.multi_org else DEFAULT_ORG_ID


# --- Handler-level tests ------------------------------------------------


@pytest.mark.parametrize(
    "multi_org", [False, True], ids=["one-org gateway", "several-orgs gateway"],
)
async def test_resource_read_is_refused_once_the_server_is_switched_off_for_the_role(
    tmp_path: Path, multi_org: bool,
) -> None:
    """``resources/read`` on a server switched off for the user's role is
    refused, never reaches the server, and writes one ``denied`` audit
    row; it worked before the switch. The role's other server, and
    another role that keeps the server on, still read, with no denied
    row."""
    gateway = await make_gateway(tmp_path, multi_org=multi_org)
    org = org_context_for(gateway)
    assert await read_resource(gateway.server, org, ALICE, NOTION) == "notion resource text"

    await switch_off_for_role(gateway, "developer", NOTION)
    gateway.upstreams.received.clear()

    assert await read_resource(gateway.server, org, ALICE, NOTION) == disabled_refusal(NOTION, ALICE)
    assert gateway.upstreams.received == []
    assert await read_resource(gateway.server, org, ALICE, GITHUB) == "github resource text"
    assert await read_resource(gateway.server, org, LENA, NOTION) == "notion resource text"
    assert gateway.upstreams.received == [
        (ALICE, GITHUB, RESOURCE_URIS[GITHUB]),
        (LENA, NOTION, RESOURCE_URIS[NOTION]),
    ]
    assert gateway.upstreams.denied == [
        make_denied_row(ALICE, NOTION, resource_row_name(NOTION)),
    ]


@pytest.mark.parametrize(
    "multi_org", [False, True], ids=["one-org gateway", "several-orgs gateway"],
)
async def test_prompt_get_is_refused_once_the_server_is_switched_off_for_the_role(
    tmp_path: Path, multi_org: bool,
) -> None:
    """``prompts/get`` on a server switched off for the user's role is
    refused, never reaches the server, and writes one ``denied`` audit
    row; it worked before the switch. The role's other server, and
    another role that keeps the server on, still render prompts, with
    no denied row."""
    gateway = await make_gateway(tmp_path, multi_org=multi_org)
    org = org_context_for(gateway)
    assert await get_prompt(gateway.server, org, ALICE, NOTION) == "notion prompt text"

    await switch_off_for_role(gateway, "developer", NOTION)
    gateway.upstreams.received.clear()

    assert await get_prompt(gateway.server, org, ALICE, NOTION) == disabled_refusal(NOTION, ALICE)
    assert gateway.upstreams.received == []
    assert await get_prompt(gateway.server, org, ALICE, GITHUB) == "github prompt text"
    assert await get_prompt(gateway.server, org, LENA, NOTION) == "notion prompt text"
    assert gateway.upstreams.received == [
        (ALICE, GITHUB, PROMPT_NAMES[GITHUB]),
        (LENA, NOTION, PROMPT_NAMES[NOTION]),
    ]
    assert gateway.upstreams.denied == [
        make_denied_row(ALICE, NOTION, prompt_row_name(NOTION)),
    ]


# --- End to end through the Access page -----------------------------------

ADMIN = "admin@example.com"


def make_standalone_settings(tmp_path: Path) -> Settings:
    """Standalone app: Notion is a per-user sign-in server, on for the
    ``developer`` role that Alice has."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {NOTION: {"url": "http://localhost:9001/mcp"}},
    }))
    config = tmp_path / "config.json"
    config_text = json.dumps({
        "upstreams": {NOTION: {"display_name": "Notion", "auth_mode": "per_user_oauth"}},
        "roles": {
            "admin": {
                "is_admin": True,
                "settings": {"mcp_access": {"mcps": {NOTION: True}}},
            },
            "developer": {
                "is_default": True,
                "settings": {"mcp_access": {"mcps": {NOTION: True}}},
            },
        },
        "users": {ADMIN: {"role": "admin"}, ALICE: {"role": "developer"}},
    })
    config.write_text(config_text)
    data_dir = tmp_path / "data"
    # Both accepted their invitation: a pending one gives no access.
    make_config_users_accepted(data_dir, config_text)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )


def test_access_page_switch_off_reaches_the_gateway_at_once(tmp_path: Path) -> None:
    """End to end on the standalone app: right after the admin switches
    Notion off for the developer role on the Access page, the gateway
    refuses Alice's ``resources/read`` and ``prompts/get`` for Notion,
    with no restart, and the Audit page lists both refusals."""
    client = TestClient(create_app(make_standalone_settings(tmp_path)))
    login_as(client, ADMIN)
    app_state = client.app.state  # type: ignore[attr-defined]
    server = create_mcp_server(app_state.runtime_manager, org_service=app_state.org_service)
    refusal = disabled_refusal(NOTION, ALICE)
    # Before the switch the reads get past the access check (the router
    # then answers that Alice has not signed in to Notion yet).
    assert asyncio.run(read_resource(server, DEFAULT_ORG_ID, ALICE, NOTION)) != refusal
    assert asyncio.run(get_prompt(server, DEFAULT_ORG_ID, ALICE, NOTION)) != refusal

    resp = client.put(f"/api/admin/roles/developer/mcps/{NOTION}", json={"enabled": False})
    assert resp.status_code == 200, resp.text

    assert asyncio.run(read_resource(server, DEFAULT_ORG_ID, ALICE, NOTION)) == refusal
    assert asyncio.run(get_prompt(server, DEFAULT_ORG_ID, ALICE, NOTION)) == refusal

    audit = client.get("/api/admin/audit", params={"mcp_id": NOTION})
    assert audit.status_code == 200, audit.text
    denied = [
        (e["action"], e["user_id"], e["tool"], e["error_message"], e["policy_rule"])
        for e in audit.json()["entries"]
        if e["policy_decision"] == "denied"
    ]
    reason = disabled_reason(NOTION, ALICE)
    assert sorted(denied) == sorted([
        ("tool_call", ALICE, resource_row_name(NOTION), reason, "mcp_disabled"),
        ("tool_call", ALICE, prompt_row_name(NOTION), reason, "mcp_disabled"),
    ])
