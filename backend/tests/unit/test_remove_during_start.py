"""Removing an MCP while its Start is on the way leaves nothing running.

Start and Stop of one MCP run one at a time (``stop_start_lock``); a
removal did not take that lock. A removal that landed in the middle of a
Start (between its checks and its launch) tore the MCP down first; the
Start then registered a fresh "connecting" record for an MCP that no
longer existed and connected it: a new sandbox and its tools back in the
catalog, for a removed server no dashboard button could reach, while the
Start reported success.

Now the removal holds the lock, the Start checks the MCP still exists
under it, and a removed MCP counts as stopped for good (``is_removed``),
so nothing that still holds its definition reopens it.

Harness: the shared ``UpstreamAdminService`` both doors call (and the
Admin MCP for the door-level check), real file-backed stores, and a real
``UpstreamClientManager`` over ``FakeSandboxService`` (a real MCP server
over memory streams that records every sandbox it opens).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock

import httpx
import pytest

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
    UpstreamStopped,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.admin_actions import NotFound
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.upstream_admin_service import (
    StartOutcome,
    StartResult,
    UpstreamAdminService,
)
from tests.unit._shared_session_harness import make_manager
from tests.unit.factories import Gate, GatedConnectionStore, make_upstream_definition
from tests.unit.fake_sandbox_service import (
    FakeSandboxService,
    make_fake_sandbox_service,
)
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    AdminParts,
    ConnectsThenPausesClientManager,
    _call,  # pyright: ignore[reportPrivateUsage]
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
    make_oauth_token,
    make_oauth_upstream_config,
)


def make_fake_backed_manager(fake: FakeSandboxService) -> UpstreamClientManager:
    return UpstreamClientManager(
        upstreams=[],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
    )


class GateBeforeLock(asyncio.Lock):
    """A Stop/Start lock whose acquire first waits at ``gate``: the caller
    has done its checks and is about to take the lock."""

    def __init__(self, lock: asyncio.Lock, gate: Gate) -> None:
        super().__init__()
        self._lock = lock
        self._gate = gate

    async def acquire(self) -> Literal[True]:
        await self._gate.hold()
        return await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class SignsInThenPausesClientManager(UpstreamClientManager):
    """A sign-in whose session connects, then pauses before the sign-in
    returns, so a removal can land in between."""

    def __init__(self) -> None:
        super().__init__([])
        self.connected = asyncio.Event()
        self.release = asyncio.Event()

    async def replace_user_session(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        *,
        auth: httpx.Auth | None = None,
        bearer_token: str | None = None,
    ) -> Any:
        del upstream, user_id, auth, bearer_token
        self.connected.set()
        await self.release.wait()
        return MagicMock()


class StartWaitsBeforeTheLock(UpstreamClientManager):
    """The first Stop/Start lock taken (the Start's) waits at ``gate``
    before it locks, so a removal can run in between."""

    def __init__(self, fake: FakeSandboxService) -> None:
        super().__init__(
            upstreams=[],
            sandbox_resolver=SandboxResolver(global_provider="e2b"),
            sandbox_services={"e2b": fake},
        )
        self.gate = Gate()
        self._first = True

    def stop_start_lock(self, upstream_id: str) -> asyncio.Lock:
        lock = super().stop_start_lock(upstream_id)
        if not self._first:
            return lock
        self._first = False
        return GateBeforeLock(lock, self.gate)


async def make_admin(
    tmp_path: Path,
    manager: UpstreamClientManager,
    connection_store: FileConnectionStore | None = None,
) -> tuple[AdminParts, UpstreamAdminService]:
    cfg, mcps = _config_with_one_stdio()
    parts = await make_admin_parts(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
        connection_store=connection_store,
    )
    return parts, UpstreamAdminService(parts.action_deps)


async def how_it_ended(outcome: StartOutcome) -> StartResult | None:
    """What the background Start ended with; ``None`` when a Stop or a
    removal cancelled it."""
    started = outcome.started
    assert started is not None
    await asyncio.wait({started}, timeout=10)
    assert started.done(), "the Start never ended"
    return None if started.cancelled() else started.result()


async def what_still_runs(
    parts: AdminParts, manager: UpstreamClientManager, fake: FakeSandboxService,
) -> list[str]:
    """Anything of ``s0`` still alive: its state record, an open sandbox
    session, its tools in the catalog."""
    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    alive: list[str] = []
    state = manager.get_state("s0")
    if state is not None:
        alive.append(f"state {state.state}")
    alive += [
        f"open sandbox session {handle.session_id}"
        for handle in fake.sessions if not handle.closed
    ]
    alive += [
        f"tool {tool.prefixed_name}"
        for tool in runtime.tool_registry.get_all_tools()
        if tool.upstream_id == "s0"
    ]
    return alive


async def audited_starts(parts: AdminParts) -> list[str]:
    rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=20)
    return [str(r["outcome"]) for r in rows if r["action"] == "reconnect"]


async def test_a_removal_during_a_start_leaves_nothing_running(
    tmp_path: Path,
) -> None:
    """The removal waits for the Start under way, then takes it down."""
    fake = make_fake_sandbox_service()
    manager = make_fake_backed_manager(fake)
    # Holds the Start right after its disconnect, before it saves itself
    # started and launches its connect.
    store = GatedConnectionStore(tmp_path, "clear_connection_error")
    parts, service = await make_admin(tmp_path, manager, store)
    try:
        start = asyncio.create_task(
            service.start_upstream(DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL),
        )
        await asyncio.wait_for(store.gate.reached.wait(), 5)
        # A second admin (or a parallel Admin MCP call) removes it. Given
        # the time to finish, it waits for the Start under way instead.
        removal = asyncio.create_task(
            service.remove_upstream(DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL),
        )
        finished_first, _ = await asyncio.wait({removal}, timeout=0.2)
        store.gate.release.set()
        outcome = await asyncio.wait_for(start, 5)
        await asyncio.wait_for(removal, 10)
        ended = await how_it_ended(outcome)

        assert not finished_first, "the removal ran in the middle of the Start"
        assert ended is None or not ended.started, ended
        assert await what_still_runs(parts, manager, fake) == []
        assert "success" not in await audited_starts(parts)
    finally:
        store.gate.release.set()
        await manager.stop_all()


async def test_a_start_that_checked_before_a_removal_finds_it_removed(
    tmp_path: Path,
) -> None:
    """The Start read the MCP, then the removal ran before the Start took
    the Stop/Start lock: the Start must not bring it back."""
    fake = make_fake_sandbox_service()
    manager = StartWaitsBeforeTheLock(fake)
    parts, service = await make_admin(tmp_path, manager)
    try:
        start = asyncio.create_task(
            service.start_upstream(DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL),
        )
        await asyncio.wait_for(manager.gate.reached.wait(), 5)
        await service.remove_upstream(DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL)
        manager.gate.release.set()

        with pytest.raises(NotFound):
            await asyncio.wait_for(start, 5)
        assert fake.session_open_count == 0
        assert await what_still_runs(parts, manager, fake) == []
    finally:
        manager.gate.release.set()
        await manager.stop_all()


async def test_an_oauth_connect_that_checked_before_a_removal_finds_it_removed(
    tmp_path: Path,
) -> None:
    """Same race on an OAuth MCP: the Connect read it and checked the
    sign-in slot, then the removal ran. Lifting its Stop would save a
    stale "started" row; signing in would start a sign-in for a removed
    MCP."""
    config, mcp_servers = make_oauth_upstream_config()
    store = GatedConnectionStore(tmp_path, "get_user_token")
    manager = UpstreamClientManager([])
    coordinator = PendingAuthCoordinator(b"k" * 32)
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager, connection_store=store,
        auth_coordinator=coordinator,
    )
    service = UpstreamAdminService(parts.action_deps)
    try:
        connect = asyncio.create_task(
            service.connect_upstream(DEFAULT_ORG_ID, "notion", actor=ADMIN_EMAIL),
        )
        await asyncio.wait_for(store.gate.reached.wait(), 5)
        await service.remove_upstream(DEFAULT_ORG_ID, "notion", actor=ADMIN_EMAIL)
        store.gate.release.set()

        with pytest.raises(NotFound):
            await asyncio.wait_for(connect, 5)
        assert manager.get_state("notion") is None
    finally:
        store.gate.release.set()
        await manager.stop_all()


async def test_an_oauth_connect_that_signed_in_before_a_removal_saves_nothing(
    tmp_path: Path,
) -> None:
    """The Connect's session went live from the admin's saved sign-in,
    then the removal ran before the Connect recorded its outcome. The
    Connect must not save anything for the removed MCP (an MCP added
    later under the same id would inherit it) nor audit a success."""
    config, mcp_servers = make_oauth_upstream_config()
    store = FileConnectionStore(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, ADMIN_EMAIL, "notion", make_oauth_token(),
    )
    manager = SignsInThenPausesClientManager()
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager, connection_store=store,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )
    service = UpstreamAdminService(parts.action_deps)
    try:
        connect = asyncio.create_task(
            service.connect_upstream(DEFAULT_ORG_ID, "notion", actor=ADMIN_EMAIL),
        )
        await asyncio.wait_for(manager.connected.wait(), 5)
        await service.remove_upstream(DEFAULT_ORG_ID, "notion", actor=ADMIN_EMAIL)
        manager.release.set()
        result = await asyncio.wait_for(connect, 5)

        assert result.aborted and not result.connected, result
        assert await store.get_started_config_hash(DEFAULT_ORG_ID, "notion") is None
        rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=20)
        assert [r["outcome"] for r in rows if r["action"] == "connect"] == [
            "aborted",
        ]
    finally:
        manager.release.set()
        await manager.stop_all()


async def test_a_removed_server_is_not_reopened_by_a_late_caller() -> None:
    """Anything still holding the definition (a tool call's reconnect, a
    heal, a delayed refresh) is refused, and opens no sandbox."""
    fake = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="gone", command="ignored")
    manager = make_manager(upstream, fake)
    try:
        await manager.connect_upstream(upstream)
        opened_before = fake.session_open_count

        await manager.unregister_upstream(upstream.id)
        with pytest.raises(UpstreamStopped):
            await manager.ensure_shared_connected(upstream)

        assert fake.session_open_count == opened_before
        assert manager.get_state(upstream.id) is None
    finally:
        await manager.stop_all()


async def test_a_removed_server_takes_its_server_logs_with_it() -> None:
    """An MCP added again under a removed one's id is a new MCP: its log
    view must not show the removed MCP's server logs."""
    fake = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="gone", command="ignored")
    manager = make_manager(upstream, fake)
    try:
        await manager.connect_upstream(upstream)
        manager.log_buffers.get_or_create(upstream.id).write("old MCP line")

        await manager.unregister_upstream(upstream.id)
        manager.register_upstream(upstream)

        assert manager.get_log_output(upstream.id) is None
    finally:
        await manager.stop_all()


async def test_an_upstream_added_again_after_its_removal_starts(
    tmp_path: Path,
) -> None:
    """The removal mark is for the removed upstream only: the same id
    added again is a new MCP, which a Start opens."""
    fake = make_fake_sandbox_service()
    manager = make_fake_backed_manager(fake)
    parts, service = await make_admin(tmp_path, manager)
    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    try:
        upstream = await runtime.config_service.get_upstream(DEFAULT_ORG_ID, "s0")
        assert upstream is not None
        await service.remove_upstream(DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL)
        await runtime.config_service.add_upstream(DEFAULT_ORG_ID, upstream)

        outcome = await service.start_upstream(
            DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL,
        )
        ended = await how_it_ended(outcome)

        assert ended is not None and ended.started, ended
        assert not manager.is_removed("s0")
    finally:
        await manager.stop_all()


async def test_admin_mcp_start_interrupted_by_a_removal_does_not_claim_it_started(
    tmp_path: Path,
) -> None:
    """The Start's connect went live, then the removal landed before the
    Start answered: it must say it was interrupted, and audit no
    successful Start for a server another admin removed."""
    cfg, mcps = _config_with_one_stdio()
    manager = ConnectsThenPausesClientManager()
    parts = await make_admin_parts(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )

    start = asyncio.create_task(
        _call(parts.server, "start_upstream", {"mcp_id": "s0"}),
    )
    await asyncio.wait_for(manager.connected.wait(), 5)
    removed = await _call(parts.server, "remove_upstream", {"mcp_id": "s0"})
    manager.release.set()
    text = await asyncio.wait_for(start, 10)

    assert removed == "Upstream MCP 's0' removed."
    assert "interrupted" in text, text
    assert "success" not in await audited_starts(parts)
