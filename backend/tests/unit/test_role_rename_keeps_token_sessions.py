"""A role rename leaves a service token's open gateway session working.

A service token holds its role by name, and the gateway reads a token's
role from the bearer's auth scopes. The MCP SDK runs every request of a
session in the task opened at ``initialize``, with the auth of that first
request: after a rename, an open session kept the old role name, which
matches no role, and listed zero tools until the client happened to open
a new session. The rename's own "tools changed" notice made the client
list again, and see nothing.

The gateway now reads the role from the bearer of the request being
handled, which the verifier resolves from the token registry on every
request. Driven through the real cloud gateway app: bearer auth, org pin,
session owner guard and the SDK's session manager.
"""
from __future__ import annotations

from pathlib import Path

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.routing import Mount

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.app import _build_mcp_app_with_oauth  # pyright: ignore[reportPrivateUsage]
from mcpolis.entrypoints.controllers.gateway_controller import create_mcp_server
from tests.unit.factories import make_full_access_config
from tests.unit.test_cloud_mcp_session_owner import (
    ACME,
    ACME_ID,
    ALICE,
    BASE_URL,
    make_client,
    make_cloud_settings,
    make_org_repo,
    make_org_service,
    mount_behind_org_context,
)
from tests.unit.test_gateway_session_owner import (
    open_session,
    send_request,
    tool_names,
)
from tests.unit.test_multi_org_gateway import (
    InMemoryOAuthStateRepository,
    make_runtime_manager_with_orgs,
)


async def test_a_token_session_opened_before_a_rename_keeps_its_tools(
    tmp_path: Path,
) -> None:
    org_service = make_org_service(make_org_repo())
    runtime_manager = make_runtime_manager_with_orgs(
        [(ACME_ID, ["notion"])], user_emails=[ALICE],
    )
    tokens = ServiceTokenService(FileServiceTokenRepository(tmp_path))
    session_manager = StreamableHTTPSessionManager(
        app=create_mcp_server(runtime_manager, org_service=org_service),
    )
    mcp_app = _build_mcp_app_with_oauth(
        session_manager,
        make_cloud_settings(),
        runtime_manager,
        InMemoryOAuthStateRepository(),
        org_service=org_service,
        service_token_service=tokens,
        session_registry=GatewaySessionRegistry(),
    )
    parent = mount_behind_org_context([Mount("/mcp", app=mcp_app)], org_service)
    minted = await tokens.mint(
        org_id=ACME_ID, label="nightly", role_name="default", created_by=ALICE,
    )
    url = f"{BASE_URL}/mcp/{ACME}/"
    async with session_manager.run(), make_client(parent) as client:
        old_session = await open_session(client, url, minted.raw_token)
        before = tool_names(await send_request(
            client, url, minted.raw_token, old_session, "tools/list",
        ))
        # What the role rename does: the running policy, then the tokens.
        runtime = runtime_manager.get_cached(ACME_ID)
        assert runtime is not None
        runtime.policy_engine.reload(
            make_full_access_config(["notion"], [ALICE], "staff"),
        )
        await tokens.rename_role(ACME_ID, "default", "staff")

        on_old = tool_names(await send_request(
            client, url, minted.raw_token, old_session, "tools/list",
        ))
        new_session = await open_session(client, url, minted.raw_token)
        on_new = tool_names(await send_request(
            client, url, minted.raw_token, new_session, "tools/list",
        ))

    assert before == ["notion__do_thing"]
    assert on_new == ["notion__do_thing"]
    assert on_old == ["notion__do_thing"], (
        "the session opened before the rename lost every tool"
    )
