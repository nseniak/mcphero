"""Cloud mode: every MCP session belongs to the caller and org that opened it.

Covers the three session-keeping MCP endpoints in their real cloud
geometry (``OrgContextMiddleware`` in front, two orgs):

- the ``/mcp`` gateway, per-org URL and bare multi-org URL;
- the ``/admin-mcp/{slug}`` admin MCP;
- the ``/admin-mcp/system`` superadmin MCP.

Each runs a session's requests inside the task the SDK started at
``initialize``, with the identity AND org captured then. So without an
owner check, an admin of their own org presenting another org's admin
session id through their own org's URL administered the other org, and
a gateway user borrowed another user's session (tools, per-user upstream
sign-ins) across orgs. The answer to someone else's session must be the
unknown-session answer: 404.

ASGI-level (``httpx.ASGITransport``), no sockets. Host is ``localhost``
because FastMCP's DNS-rebinding check only admits loopback hosts.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount

from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.app import (
    _build_admin_app_with_oauth,
    _build_mcp_app_with_oauth,
    _build_superadmin_app_with_oauth,
)
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
)
from mcpolis.entrypoints.controllers.superadmin_controller import (
    create_superadmin_mcp_server,
)
from mcpolis.entrypoints.middleware.org_context import (
    OrgContextMiddleware,
    SlugCache,
)
from tests.unit._gateway_oauth_store import InMemoryOAuthStateRepository
from tests.unit.factories import make_runtime_manager
from tests.unit.test_gateway_session_owner import (
    open_session,
    rpc_result,
    send_request,
    tool_names,
)
from tests.unit.test_multi_org_gateway import (
    InMemoryOrgRepo,
    make_membership,
    make_org,
    make_runtime_manager_with_orgs,
)

BASE_URL = "http://localhost:8000"
ACME_ID, ACME = "acme-id", "acme"
BOBCO_ID, BOBCO = "bobco-id", "bobco"
ALICE = "alice@acme.test"  # acme
CAROL = "carol@acme.test"  # acme
BOB = "bob@bobco.test"  # bobco
ROOT = "root@ops.test"  # superadmin
ROOT2 = "root2@ops.test"  # superadmin
LIST_USERS = {"name": "list_users", "arguments": {}}


def make_org_repo() -> InMemoryOrgRepo:
    return InMemoryOrgRepo(
        orgs=[make_org(ACME_ID, ACME, "Acme"), make_org(BOBCO_ID, BOBCO, "Bobco")],
        memberships=[
            make_membership(ACME_ID, ALICE),
            make_membership(ACME_ID, CAROL),
            make_membership(BOBCO_ID, BOB),
        ],
    )


def make_org_service(org_repo: InMemoryOrgRepo) -> OrgService:
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig())
    return OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]


def make_cloud_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        server_url=BASE_URL,
    )


def make_provider(runtime_manager: OrgRuntimeManager) -> McpGatewayOAuthProvider:
    return McpGatewayOAuthProvider(
        google_client_id="",
        google_client_secret="",
        server_url=BASE_URL,
        runtime_manager=runtime_manager,
        state_repository=InMemoryOAuthStateRepository(),
    )


def merge_runtime_managers(
    first: OrgRuntimeManager, *others: OrgRuntimeManager,
) -> OrgRuntimeManager:
    """One manager holding every runtime of ``others`` too."""
    for other in others:
        first._runtimes.update(other._runtimes)
        first._startup_status.update(other._startup_status)
    return first


def make_admin_config(admins: list[str]) -> SettingsConfig:
    return SettingsConfig(
        roles={"admin": RoleDefinition(is_admin=True, settings=RoleSettings())},
        users={email: UserDefinition(role="admin") for email in admins},
    )


def mount_behind_org_context(
    routes: list[BaseRoute], org_service: OrgService,
) -> Starlette:
    parent = Starlette(routes=routes)
    parent.add_middleware(
        OrgContextMiddleware,
        settings=make_cloud_settings(),
        org_service=org_service,
        slug_cache=SlugCache(),
    )
    return parent


def make_client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        timeout=30.0,
    )


def make_cloud_gateway(
    tmp_path: Path,
) -> tuple[Starlette, StreamableHTTPSessionManager, McpGatewayOAuthProvider]:
    """The real gateway app for two orgs: acme (alice, carol, upstream
    ``notion``) and bobco (bob, upstream ``linear``)."""
    org_service = make_org_service(make_org_repo())
    runtime_manager = merge_runtime_managers(
        make_runtime_manager_with_orgs(
            [(ACME_ID, ["notion"])], user_emails=[ALICE, CAROL],
        ),
        make_runtime_manager_with_orgs(
            [(BOBCO_ID, ["linear"])], user_emails=[BOB],
        ),
    )
    session_manager = StreamableHTTPSessionManager(
        app=create_mcp_server(runtime_manager, org_service=org_service),
    )
    mcp_app = _build_mcp_app_with_oauth(
        session_manager,
        make_cloud_settings(),
        runtime_manager,
        InMemoryOAuthStateRepository(),
        org_service=org_service,
        service_token_service=ServiceTokenService(
            FileServiceTokenRepository(tmp_path),
        ),
        session_registry=GatewaySessionRegistry(),
    )
    parent = mount_behind_org_context([Mount("/mcp", app=mcp_app)], org_service)
    return parent, session_manager, mcp_app.state.mcp_gateway_oauth_provider


class CloudAdminMcp(NamedTuple):
    app: Starlette
    admin_mcp: FastMCP
    provider: McpGatewayOAuthProvider
    runtime_manager: OrgRuntimeManager


def make_cloud_admin_mcp(
    tmp_path: Path,
    acme_admins: tuple[str, ...] = (ALICE, CAROL),
    bobco_admins: tuple[str, ...] = (BOB,),
) -> CloudAdminMcp:
    """The real admin MCP app for two orgs, acme and bobco."""
    org_service = make_org_service(make_org_repo())
    runtime_manager = merge_runtime_managers(
        make_runtime_manager(
            PolicyEngine(make_admin_config(list(acme_admins))), org_id=ACME_ID,
        ),
        make_runtime_manager(
            PolicyEngine(make_admin_config(list(bobco_admins))), org_id=BOBCO_ID,
        ),
    )
    provider = make_provider(runtime_manager)
    admin_mcp = create_admin_mcp_server(
        runtime_manager=runtime_manager,
        audit_repo=FileAuditRepository(tmp_path / "audit.jsonl"),
        policy_store=FileConfigStore(tmp_path / "config.json"),
        template_var_repo=FileTemplateVarRepository(tmp_path),
    )
    guarded = _build_admin_app_with_oauth(
        admin_mcp,
        provider,
        make_cloud_settings(),
        runtime_manager,
    )
    parent = mount_behind_org_context(
        [Mount("/admin-mcp", app=guarded)], org_service,
    )
    return CloudAdminMcp(parent, admin_mcp, provider, runtime_manager)


@pytest.mark.asyncio
async def test_gateway_session_is_bound_to_its_person_and_org(
    tmp_path: Path,
) -> None:
    """Per-org URL: bob can't run alice's acme session, neither through
    his own org's URL nor through acme's; alice herself can't run it
    through another org's URL; alice where she opened it can."""
    parent, session_manager, provider = make_cloud_gateway(tmp_path)
    alice = await provider.mint_test_token(ALICE)
    bob = await provider.mint_test_token(BOB)
    acme_url = f"{BASE_URL}/mcp/{ACME}/"
    bobco_url = f"{BASE_URL}/mcp/{BOBCO}/"
    async with session_manager.run(), make_client(parent) as client:
        alice_session = await open_session(client, acme_url, alice)
        bob_session = await open_session(client, bobco_url, bob)
        assert tool_names(await send_request(
            client, bobco_url, bob, bob_session, "tools/list",
        )) == ["linear__do_thing"]

        for url, bearer in (
            (bobco_url, bob), (acme_url, bob), (bobco_url, alice),
        ):
            borrowed = await send_request(
                client, url, bearer, alice_session, "tools/list",
            )
            assert borrowed.status_code == 404, (url, borrowed.text)

        assert tool_names(await send_request(
            client, acme_url, alice, alice_session, "tools/list",
        )) == ["notion__do_thing"]


@pytest.mark.asyncio
async def test_bare_gateway_session_is_bound_to_its_person(
    tmp_path: Path,
) -> None:
    """Bare ``/mcp`` (every org of the caller merged): bob's bearer on
    alice's session does not get alice's orgs' tools."""
    parent, session_manager, provider = make_cloud_gateway(tmp_path)
    alice = await provider.mint_test_token(ALICE)
    bob = await provider.mint_test_token(BOB)
    url = f"{BASE_URL}/mcp/"
    async with session_manager.run(), make_client(parent) as client:
        alice_session = await open_session(client, url, alice)

        borrowed = await send_request(
            client, url, bob, alice_session, "tools/list",
        )
        assert borrowed.status_code == 404, borrowed.text

        assert tool_names(await send_request(
            client, url, alice, alice_session, "tools/list",
        )) != []


@pytest.mark.asyncio
async def test_admin_mcp_session_is_bound_to_its_admin_and_org(
    tmp_path: Path,
) -> None:
    """Bob administers bobco. Alice's acme admin session must not let him
    administer acme through his own org's URL. Carol, a second acme
    admin, can't borrow it either. Alice can."""
    parent, admin_mcp, provider, _ = make_cloud_admin_mcp(tmp_path)
    alice = await provider.mint_test_token(ALICE)
    carol = await provider.mint_test_token(CAROL)
    bob = await provider.mint_test_token(BOB)
    acme_url = f"{BASE_URL}/admin-mcp/{ACME}/"
    bobco_url = f"{BASE_URL}/admin-mcp/{BOBCO}/"
    async with admin_mcp.session_manager.run(), make_client(parent) as client:
        alice_session = await open_session(client, acme_url, alice)

        by_bob = await send_request(
            client, bobco_url, bob, alice_session, "tools/call", LIST_USERS,
        )
        assert by_bob.status_code == 404, by_bob.text
        by_carol = await send_request(
            client, acme_url, carol, alice_session, "tools/call", LIST_USERS,
        )
        assert by_carol.status_code == 404, by_carol.text

        by_alice = await send_request(
            client, acme_url, alice, alice_session, "tools/call", LIST_USERS,
        )
        users = json.loads(rpc_result(by_alice)["content"][0]["text"])
        assert {user["email"] for user in users} == {ALICE, CAROL}


@pytest.mark.asyncio
async def test_admin_of_both_orgs_cannot_carry_a_session_across_orgs(
    tmp_path: Path,
) -> None:
    """The org half of the owner on the admin MCP. Alice administers both
    orgs and opens an acme admin session; acme then demotes her. Through
    acme's URL the role check refuses her (403). Through bobco's URL,
    where she is still admin, only the org binding stops the session
    from running as acme and listing acme's users."""
    parent, admin_mcp, provider, runtime_manager = make_cloud_admin_mcp(
        tmp_path, acme_admins=(ALICE, CAROL), bobco_admins=(BOB, ALICE),
    )
    alice = await provider.mint_test_token(ALICE)
    acme_url = f"{BASE_URL}/admin-mcp/{ACME}/"
    bobco_url = f"{BASE_URL}/admin-mcp/{BOBCO}/"
    async with admin_mcp.session_manager.run(), make_client(parent) as client:
        acme_session = await open_session(client, acme_url, alice)
        acme_runtime = runtime_manager.get_cached(ACME_ID)
        assert acme_runtime is not None
        acme_runtime.policy_engine.reload(make_admin_config([CAROL]))

        via_acme = await send_request(
            client, acme_url, alice, acme_session, "tools/call", LIST_USERS,
        )
        assert via_acme.status_code == 403, via_acme.text
        via_bobco = await send_request(
            client, bobco_url, alice, acme_session, "tools/call", LIST_USERS,
        )
        assert via_bobco.status_code == 404, via_bobco.text


@pytest.mark.asyncio
async def test_admin_mcp_foreign_session_gets_the_unknown_session_answer(
    tmp_path: Path,
) -> None:
    """Anti-enumeration on the admin MCP: another org's live session id
    and an id that never existed answer the same."""
    parent, admin_mcp, provider, _ = make_cloud_admin_mcp(tmp_path)
    alice = await provider.mint_test_token(ALICE)
    bob = await provider.mint_test_token(BOB)
    acme_url = f"{BASE_URL}/admin-mcp/{ACME}/"
    bobco_url = f"{BASE_URL}/admin-mcp/{BOBCO}/"
    async with admin_mcp.session_manager.run(), make_client(parent) as client:
        alice_session = await open_session(client, acme_url, alice)

        foreign = await send_request(
            client, bobco_url, bob, alice_session, "tools/list",
        )
        unknown = await send_request(
            client, bobco_url, bob, uuid.uuid4().hex, "tools/list",
        )
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.headers.get("content-type") == (
            unknown.headers.get("content-type")
        )
        assert foreign.text == unknown.text


@pytest.mark.asyncio
async def test_superadmin_mcp_session_is_bound_to_its_superadmin(
    tmp_path: Path,
) -> None:
    """Two allowlisted superadmins: neither runs the other's session."""
    org_repo = make_org_repo()
    runtime_manager = make_runtime_manager(PolicyEngine(SettingsConfig()))
    provider = make_provider(runtime_manager)
    superadmin_mcp = create_superadmin_mcp_server(
        org_repo=org_repo,  # type: ignore[arg-type]
        runtime_manager=runtime_manager,
        org_service=make_org_service(org_repo),
    )
    guarded = _build_superadmin_app_with_oauth(
        superadmin_mcp,
        provider,
        make_cloud_settings(),
        EmailAllowlist([ROOT, ROOT2]),
    )
    parent = Starlette(routes=[Mount("/admin-mcp/system", app=guarded)])
    root = await provider.mint_test_token(ROOT)
    root2 = await provider.mint_test_token(ROOT2)
    url = f"{BASE_URL}/admin-mcp/system/"
    async with superadmin_mcp.session_manager.run(), make_client(
        parent,
    ) as client:
        root_session = await open_session(client, url, root)

        borrowed = await send_request(
            client, url, root2, root_session, "tools/list",
        )
        assert borrowed.status_code == 404, borrowed.text

        assert "list_organizations" in tool_names(await send_request(
            client, url, root, root_session, "tools/list",
        ))
