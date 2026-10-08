"""An outsider's ``initialize`` on ``/mcp/{slug}`` must not list the org's
MCPs.

Any signed-in account can open a session on an org's gateway URL:
membership is enforced by policy only. ``resources/read`` and
``prompts/get`` already answer an MCP the org lacks like one disabled for
the caller, so the answers can't list an org's MCPs. The ``initialize``
answer, built before any policy check, used to append a "Connected
upstreams" block naming every MCP of the org, with its display name and
self-description, whoever asked.

Over the real cloud gateway app (``OrgContextMiddleware`` in front, two
orgs), at ASGI level.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.routing import Mount

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import UpstreamSelfDescription
from mcpolis.domain.services.org_runtime import OrgRuntime
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.app import _build_mcp_app_with_oauth  # pyright: ignore[reportPrivateUsage]
from mcpolis.entrypoints.controllers.gateway_controller import create_mcp_server
from tests.unit._gateway_oauth_store import InMemoryOAuthStateRepository
from tests.unit.factories import make_full_access_config
from tests.unit.test_cloud_mcp_session_owner import (
    ACME,
    ACME_ID,
    ALICE,
    BASE_URL,
    BOB,
    BOBCO_ID,
    make_client,
    make_cloud_settings,
    make_org_repo,
    make_org_service,
    mount_behind_org_context,
)
from tests.unit.test_gateway_instructions_with_upstreams import (
    make_manager,
    make_runtime,
)
from tests.unit.test_gateway_session_owner import make_headers, send_request

PROTOCOL_VERSION = "2025-06-18"


def make_acme_runtime() -> OrgRuntime:
    """acme runs ``payroll-db``, which alice may use."""
    return make_runtime(
        ACME_ID,
        [(
            "payroll-db", "Payroll DB",
            UpstreamSelfDescription(
                name="payroll", version="1.0",
                description="Salaries of every Acme employee.",
            ),
        )],
        config=make_full_access_config(["payroll-db"], [ALICE]),
    )


async def initialize_on_acme(tmp_path: Path, caller: str) -> tuple[str, str]:
    """``caller`` opens a session on acme's gateway URL: the
    ``initialize`` answer's body, and the ``tools/list`` answer's."""
    org_service = make_org_service(make_org_repo())
    runtime_manager = make_manager({
        ACME_ID: make_acme_runtime(),
        BOBCO_ID: make_runtime(BOBCO_ID, [], config=SettingsConfig()),
    })
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
    provider = mcp_app.state.mcp_gateway_oauth_provider
    parent = mount_behind_org_context([Mount("/mcp", app=mcp_app)], org_service)
    bearer = await provider.mint_test_token(caller)
    url = f"{BASE_URL}/mcp/{ACME}/"
    async with session_manager.run(), make_client(parent) as client:
        init = await post_initialize(client, url, bearer)
        session_id = init.headers["mcp-session-id"]
        await client.post(
            url, headers=make_headers(bearer, session_id),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools = await send_request(client, url, bearer, session_id, "tools/list")
    return init.text, tools.text


async def post_initialize(
    client: httpx.AsyncClient, url: str, bearer: str,
) -> httpx.Response:
    response = await client.post(
        url,
        headers=make_headers(bearer),
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    assert response.status_code == 200, response.text
    return response


def instructions_in(body: str) -> str:
    """The ``instructions`` of an ``initialize`` answer sent as an SSE
    event."""
    data = next(
        line.removeprefix("data: ")
        for line in body.splitlines() if line.startswith("data: ")
    )
    return str(json.loads(data)["result"].get("instructions", ""))


async def test_an_outsiders_initialize_does_not_list_the_orgs_mcps(
    tmp_path: Path,
) -> None:
    """bob, a member of bobco only, opens a session on acme's URL: no
    tools, and no acme MCP in the instructions."""
    init, tools = await initialize_on_acme(tmp_path, BOB)

    assert '"tools":[]' in tools.replace(" ", ""), tools
    assert "payroll-db" not in init and "Salaries" not in init, init


async def test_a_members_initialize_lists_the_mcps_they_may_use(
    tmp_path: Path,
) -> None:
    init, _ = await initialize_on_acme(tmp_path, ALICE)

    assert "payroll-db (Payroll DB): Salaries of every Acme employee." in (
        instructions_in(init)
    )
