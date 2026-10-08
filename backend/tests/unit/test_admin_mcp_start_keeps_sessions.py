"""The Admin MCP's ``start_upstream`` and ``connect_upstream`` on OAuth and
hosted MCPs: what they leave running.

- ``start_upstream`` is annotated non-destructive, but on an OAuth MCP it
  disconnected first, and since Stop closes every member's session that
  cut off every member. An OAuth Start now works like Connect.
- After an admin's ``disconnect_upstream`` (Stop, sign-ins kept), another
  admin's ``connect_upstream`` or ``start_upstream`` brings it back from
  the admin sign-in the Stop kept, whoever holds it: no browser for the
  caller, no "another admin holds the sign-in" refusal.
- Two ``start_upstream`` calls at once on a hosted MCP no longer cut each
  other: the second sees the first starting.

Real streamable-HTTP MCP server on loopback for the OAuth MCPs
(``_user_session_harness``: it reports the bearer a session uses), real
file stores, the Admin MCP over its tools.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.fastmcp import FastMCP

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from tests.unit._user_session_harness import (
    ConnectionGate,
    acquire,
    make_token,
    start_upstream,
    stop_upstream,
)
from tests.unit.factories import GatedConnectionStore, make_oauth_upstream
from tests.unit.fake_sandbox_service import make_fake_sandbox_service
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    DEPUTY_EMAIL,
    AdminParts,
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
    make_bearer_user,
    make_config_two_admins,
)

MEMBER = "member@example.com"
DROP = "drop"


async def call_as(
    server: FastMCP, caller: str, name: str, args: dict[str, Any],
) -> str:
    """Invoke an Admin MCP tool as ``caller`` (a bearer token, as in
    production) and return its text."""
    org = current_org_id.set(DEFAULT_ORG_ID)
    auth = auth_context_var.set(make_bearer_user(caller))
    try:
        result: Any = await server.call_tool(name, args)
    finally:
        current_org_id.reset(org)
        auth_context_var.reset(auth)
    return str(result[0][0].text)


def make_oauth_drop(url: str, mode: AuthMode) -> UpstreamDefinition:
    return make_oauth_upstream(id=DROP, display_name="Drop", mode=mode, url=url)


async def make_oauth_admin(
    tmp_path: Path, url: str, mode: AuthMode, signed_in: list[str],
) -> tuple[AdminParts, UpstreamClientManager, FileConnectionStore]:
    """The Admin MCP of an org with two admins and the OAuth MCP ``drop``
    at ``url``; each address in ``signed_in`` holds a saved sign-in
    (bearer ``token-<name>``)."""
    config = make_config_two_admins()
    config["upstreams"] = {DROP: {"display_name": "Drop", "auth_mode": mode.value}}
    store = FileConnectionStore(tmp_path)
    for email in signed_in:
        await store.put_user_token(
            DEFAULT_ORG_ID, email, DROP, make_token(f"token-{email.split('@')[0]}"),
        )
    manager = UpstreamClientManager([])
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers={DROP: {"url": url}},
        client_manager=manager, connection_store=store,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )
    return parts, manager, store


async def bearer_of(manager: UpstreamClientManager, user: str) -> str:
    session = manager.find_user_session(DROP, user)
    assert session is not None, f"{user} has no live session"
    result = await session.call_tool("whoami", {})
    return str(getattr(result.content[0], "text", ""))


async def test_admin_mcp_start_on_a_working_oauth_server_keeps_members_sessions(
    tmp_path: Path,
) -> None:
    server, task, url = await start_upstream(ConnectionGate())
    parts, manager, store = await make_oauth_admin(
        tmp_path, url, AuthMode.per_user_oauth, [ADMIN_EMAIL, MEMBER],
    )
    try:
        member_session = await acquire(
            manager, make_oauth_drop(url, AuthMode.per_user_oauth), store, MEMBER,
        )

        text = await call_as(parts.server, ADMIN_EMAIL, "start_upstream", {"mcp_id": DROP})

        assert "is connected" in text, text
        assert manager.find_user_session(DROP, MEMBER) is member_session, (
            "the member's live session was closed by a non-destructive Start"
        )
        assert await bearer_of(manager, ADMIN_EMAIL) == "Bearer token-admin"
    finally:
        await manager.stop_all()
        await stop_upstream(server, task)


async def test_another_admins_connect_after_a_stop_uses_the_kept_sign_in(
    tmp_path: Path,
) -> None:
    server, task, url = await start_upstream(ConnectionGate())
    parts, manager, store = await make_oauth_admin(
        tmp_path, url, AuthMode.admin_oauth, [DEPUTY_EMAIL],
    )
    try:
        stopped = await call_as(
            parts.server, DEPUTY_EMAIL, "disconnect_upstream", {"mcp_id": DROP},
        )
        assert "disconnected" in stopped, stopped

        text = await call_as(parts.server, ADMIN_EMAIL, "connect_upstream", {"mcp_id": DROP})

        assert "is connected" in text, text
        assert not manager.is_stopped(DROP)
        assert await store.is_enabled(DEFAULT_ORG_ID, DROP)
        # Reconnected from the deputy's kept sign-in; the caller was
        # neither sent to a sign-in page nor signed in.
        assert await bearer_of(manager, DEPUTY_EMAIL) == "Bearer token-deputy"
        assert await store.get_user_token(DEFAULT_ORG_ID, ADMIN_EMAIL, DROP) is None
        rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=10)
        assert ("connect", "success", ADMIN_EMAIL) in [
            (r["action"], r["outcome"], r["user_id"]) for r in rows
        ]
    finally:
        await manager.stop_all()
        await stop_upstream(server, task)


async def test_another_admins_start_after_a_stop_uses_the_kept_sign_in(
    tmp_path: Path,
) -> None:
    server, task, url = await start_upstream(ConnectionGate())
    parts, manager, store = await make_oauth_admin(
        tmp_path, url, AuthMode.admin_oauth, [DEPUTY_EMAIL],
    )
    try:
        await call_as(parts.server, ADMIN_EMAIL, "disconnect_upstream", {"mcp_id": DROP})
        assert manager.is_stopped(DROP)

        text = await call_as(parts.server, ADMIN_EMAIL, "start_upstream", {"mcp_id": DROP})

        assert "is connected" in text, text
        assert not manager.is_stopped(DROP)
        assert await store.is_enabled(DEFAULT_ORG_ID, DROP)
        assert await bearer_of(manager, DEPUTY_EMAIL) == "Bearer token-deputy"
        assert await store.get_user_token(DEFAULT_ORG_ID, ADMIN_EMAIL, DROP) is None
    finally:
        await manager.stop_all()
        await stop_upstream(server, task)


async def test_two_admin_mcp_starts_at_once_do_not_cut_each_other(
    tmp_path: Path,
) -> None:
    """Both calls arrive while the MCP is stopped; the first is held after
    its checks, before its launch. The second must then see it starting,
    not start again (which cancelled the first: "interrupted")."""
    cfg, mcps = _config_with_one_stdio()
    fake = make_fake_sandbox_service()
    manager = UpstreamClientManager(
        upstreams=[],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
    )
    store = GatedConnectionStore(tmp_path, "clear_connection_error")
    parts = await make_admin_parts(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
        connection_store=store,
    )
    try:
        first = asyncio.create_task(
            call_as(parts.server, ADMIN_EMAIL, "start_upstream", {"mcp_id": "s0"}),
        )
        await asyncio.wait_for(store.gate.reached.wait(), 5)
        second = asyncio.create_task(
            call_as(parts.server, DEPUTY_EMAIL, "start_upstream", {"mcp_id": "s0"}),
        )
        await asyncio.sleep(0.05)  # the second call is on its way meanwhile
        store.gate.release.set()
        first_text, second_text = await asyncio.wait_for(
            asyncio.gather(first, second), 10,
        )

        assert first_text.startswith("MCP 's0' started."), first_text
        assert "already" in second_text, second_text
        assert fake.session_open_count == 1
    finally:
        store.gate.release.set()
        await manager.stop_all()
