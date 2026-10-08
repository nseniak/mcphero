"""Boot leaves alone a hosted MCP that a request already opened.

Any request builds its org's runtime (``OrgRuntimeManager.get``), while
the boot walk (``connect_all_background``) goes org by org. So a member's
tool call can open a hosted MCP's session, or an admin's Start can be
opening it, before boot reaches that org. Boot's first phase then marked
the MCP "Ready from cache" or "never started" anyway, and both steps drop
what was there: the tool call's session closed (its sandbox killed, the
calls on it cut) and the Start cancelled ("interrupted", and the MCP left
on "Ready from cache" instead of running).

Real ``UpstreamClientManager`` over ``FakeSandboxService`` (a real MCP
server over memory streams that records every sandbox session), real
in-memory sandbox refs; the other runtime parts are stand-ins.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
)
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamDefinition,
    UpstreamSelfDescription,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.upstream_connection_service import (
    start_shared_in_background,
)
from tests.unit.factories import Gate, make_upstream_definition
from tests.unit.fake_sandbox_service import (
    FakeSandboxService,
    make_fake_sandbox_service,
)

ORG = "acme"


class RefsHeldOnFirstRead(InMemorySandboxPersistenceRepository):
    """Sandbox refs whose first read, boot reading the MCP's cached ref,
    waits at ``gate``. Later reads go through."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = Gate()
        self._first_read = True

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        if self._first_read:
            self._first_read = False
            await self.gate.hold()
        return await super().get(org_id=org_id, upstream_id=upstream_id)


def make_hosted_mcp() -> UpstreamDefinition:
    return make_upstream_definition(id="stdio-mcp", command="npx")


def make_cached_ref(upstream_id: str) -> SandboxPersistedRef:
    """The ref a hosted MCP that ran before the restart left: proof it
    started, so boot shows it "Ready from cache"."""
    return SandboxPersistedRef(
        provider="e2b",
        org_id=ORG,
        upstream_id=upstream_id,
        mcpolis_instance="stable",
        sandbox_id="sbx-survived",
        paused_snapshot_id=None,
        pid=4242,
        metadata={},
        cached_server_info=ServerInfo(name="cached", version="1.0.0"),
        cached_self_description=UpstreamSelfDescription(
            name="cached", version="1.0.0",
        ),
        last_updated=datetime.now(UTC),
    )


async def make_refs(
    upstream: UpstreamDefinition,
    *,
    cached: bool,
    refs: InMemorySandboxPersistenceRepository | None = None,
) -> InMemorySandboxPersistenceRepository:
    refs = refs if refs is not None else InMemorySandboxPersistenceRepository()
    if cached:
        await refs.upsert(make_cached_ref(upstream.id))
    return refs


def make_runtime(
    upstream: UpstreamDefinition,
    refs: InMemorySandboxPersistenceRepository,
    fake: FakeSandboxService,
    *,
    saved_stops: set[str] | None = None,
) -> tuple[OrgRuntimeManager, OrgRuntime, UpstreamClientManager]:
    """The org's runtime, built (as a request builds it) before boot runs
    ``connect_runtime`` on it. ``saved_stops`` is what boot reads as the
    MCPs saved stopped."""
    manager = UpstreamClientManager(
        upstreams=[upstream],
        org_id=ORG,
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
        sandbox_persistence=refs,
    )
    tool_registry = MagicMock()
    tool_registry.hydrate = AsyncMock()
    tool_registry.refresh_all = AsyncMock()
    runtime = OrgRuntime(
        org_id=ORG,
        policy_engine=MagicMock(get_admin_emails=MagicMock(return_value=[])),
        tool_registry=tool_registry,
        client_manager=manager,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=[upstream],
    )
    connection_repo = MagicMock()
    connection_repo.get_disabled_ids = AsyncMock(
        return_value=saved_stops or set(),
    )
    org_manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=connection_repo,
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://127.0.0.1:8080",
    )
    return org_manager, runtime, manager


def closed_sessions(fake: FakeSandboxService) -> list[str]:
    return [handle.session_id for handle in fake.sessions if handle.closed]


async def start_in_flight(
    manager: UpstreamClientManager,
    upstream: UpstreamDefinition,
    fake: FakeSandboxService,
) -> asyncio.Task[None]:
    """An admin's Start whose sandbox is still being created (held by
    the fake's ``hold_entry``)."""
    async def connect() -> None:
        await manager.connect_upstream(upstream)

    async with manager.stop_start_lock(upstream.id):
        start = await start_shared_in_background(
            org_id=ORG,
            upstream_id=upstream.id,
            client_manager=manager,
            connection_store=None,
            connect=connect,
        )
    async with asyncio.timeout(5):
        while fake.entries_started == 0:
            await asyncio.sleep(0.01)
    assert manager.is_starting(upstream.id)
    return start


@pytest.mark.parametrize("cached", [True, False], ids=["cached_ref", "no_cache"])
async def test_boot_leaves_the_session_a_tool_call_opened_first_alone(
    cached: bool,
) -> None:
    upstream = make_hosted_mcp()
    fake = make_fake_sandbox_service()
    org_manager, runtime, manager = make_runtime(
        upstream, await make_refs(upstream, cached=cached), fake,
    )
    try:
        await manager.ensure_shared_connected(upstream)  # the tool call

        await asyncio.wait_for(org_manager.connect_runtime(runtime), 10)

        assert closed_sessions(fake) == [], (
            "boot closed the session a tool call had opened, killing its "
            "sandbox"
        )
        assert manager.is_connected(upstream.id)
    finally:
        await manager.stop_all()


@pytest.mark.parametrize("cached", [True, False], ids=["cached_ref", "no_cache"])
async def test_boot_leaves_a_session_opened_while_it_read_the_ref_alone(
    cached: bool,
) -> None:
    """The tool call lands while boot reads the MCP's cached sandbox ref:
    boot's step must check again as it writes."""
    upstream = make_hosted_mcp()
    fake = make_fake_sandbox_service()
    refs = await make_refs(upstream, cached=cached, refs=RefsHeldOnFirstRead())
    assert isinstance(refs, RefsHeldOnFirstRead)
    org_manager, runtime, manager = make_runtime(upstream, refs, fake)
    try:
        boot = asyncio.create_task(org_manager.connect_runtime(runtime))
        await asyncio.wait_for(refs.gate.reached.wait(), 5)
        await manager.ensure_shared_connected(upstream)  # the tool call
        refs.gate.release.set()
        await asyncio.wait_for(boot, 10)

        assert closed_sessions(fake) == []
        state = manager.get_state(upstream.id)
        assert state is not None and state.state == UpstreamConnectionState.LIVE, (
            state.state if state else None
        )
    finally:
        refs.gate.release.set()
        await manager.stop_all()


async def test_boot_does_not_cancel_an_admins_start_in_flight() -> None:
    upstream = make_hosted_mcp()
    hold_entry = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=hold_entry)
    org_manager, runtime, manager = make_runtime(
        upstream, await make_refs(upstream, cached=True), fake,
    )
    try:
        start = await start_in_flight(manager, upstream, fake)

        await asyncio.wait_for(org_manager.connect_runtime(runtime), 10)
        cancelled_by_boot = start.cancelled()
        hold_entry.set()
        await asyncio.wait({start}, timeout=5)

        assert not cancelled_by_boot and not start.cancelled(), (
            "boot cancelled the admin's Start"
        )
        state = manager.get_state(upstream.id)
        assert state is not None and state.state == UpstreamConnectionState.LIVE, (
            state.state if state else None
        )
    finally:
        hold_entry.set()
        await manager.stop_all()


async def test_boot_does_not_stop_an_mcp_an_admin_started_after_it_read_the_stops() -> None:
    """Boot read the MCP as saved stopped, then an admin's Start lifted
    that Stop before boot got to the MCP: the Start stands."""
    upstream = make_hosted_mcp()
    hold_entry = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=hold_entry)
    org_manager, runtime, manager = make_runtime(
        upstream, await make_refs(upstream, cached=False), fake,
        saved_stops={upstream.id},
    )
    try:
        start = await start_in_flight(manager, upstream, fake)

        await asyncio.wait_for(org_manager.connect_runtime(runtime), 10)
        stopped_by_boot = manager.is_stopped(upstream.id)
        hold_entry.set()
        await asyncio.wait({start}, timeout=5)

        assert not stopped_by_boot and not start.cancelled()
        assert manager.is_connected(upstream.id)
    finally:
        hold_entry.set()
        await manager.stop_all()


async def test_the_cloud_boot_walk_leaves_the_session_of_a_runtime_a_request_built_alone() -> None:
    """The same through the cloud boot entry point: the runtime a request
    built is in the manager's cache, and ``connect_all_background`` runs
    ``connect_runtime`` on it."""
    upstream = make_hosted_mcp()
    fake = make_fake_sandbox_service()
    org_manager, runtime, manager = make_runtime(
        upstream, await make_refs(upstream, cached=True), fake,
    )
    org_manager._runtimes[ORG] = runtime  # pyright: ignore[reportPrivateUsage]
    try:
        await manager.ensure_shared_connected(upstream)

        await asyncio.wait_for(org_manager.connect_all_background([ORG]), 10)

        assert closed_sessions(fake) == []
        assert manager.is_connected(upstream.id)
    finally:
        await manager.stop_all()
