"""A Stop that lands while boot is connecting an org stays a Stop.

Boot (``OrgRuntimeManager.connect_runtime``) reads the saved Stops once,
then walks every upstream. For a hosted stdio MCP it reads the sandbox
ref (cached metadata) and moves the upstream to DEFERRED_ATTACH ("Ready",
attach on the first call) or FAILED ("never started"). Those writes used
to replace the DISABLED an admin's Stop had set meanwhile: the dashboard
showed the stopped MCP Ready, storage said stopped, and the next tool
call opened a sandbox for it.

These tests park boot inside that read, run the admin's Stop
(``stop_keeping_sign_ins``, as the dashboard and the Admin MCP run it),
release boot, then check the upstream is still stopped and that a tool
call does not open a sandbox for it.
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
    UpstreamStopped,
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
    stop_keeping_sign_ins,
)
from tests.unit.factories import Gate, make_upstream_definition
from tests.unit.fake_sandbox_service import (
    FakeSandboxService,
    make_fake_sandbox_service,
)

ORG = "acme"


class RefReadWaitsOnce(InMemorySandboxPersistenceRepository):
    """Sandbox refs whose first ``get`` waits at ``gate``: boot reading the
    cached metadata while an admin clicks Stop."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = Gate()

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        await self.gate.hold()
        return await super().get(org_id=org_id, upstream_id=upstream_id)


def make_cached_ref(upstream_id: str) -> SandboxPersistedRef:
    """What a hosted MCP that ran before the restart left: a sandbox ref
    with the server's cached metadata."""
    return SandboxPersistedRef(
        provider="e2b",
        org_id=ORG,
        upstream_id=upstream_id,
        mcpolis_instance="prior-instance",
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


async def make_refs(upstream_id: str, *, cached: bool) -> RefReadWaitsOnce:
    refs = RefReadWaitsOnce()
    if cached:
        await refs.upsert(make_cached_ref(upstream_id))
    return refs


def make_boot(
    upstream: UpstreamDefinition,
    refs: RefReadWaitsOnce,
    fake: FakeSandboxService,
) -> tuple[OrgRuntimeManager, OrgRuntime, UpstreamClientManager]:
    """One org runtime holding ``upstream``, whose boot found no saved
    Stop."""
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
    # Nothing was stopped when boot read the saved Stops.
    connection_repo.get_disabled_ids = AsyncMock(return_value=set())
    org_manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=connection_repo,
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://127.0.0.1:8080",
    )
    return org_manager, runtime, manager


async def boot_with_a_stop_meanwhile(
    org_manager: OrgRuntimeManager,
    runtime: OrgRuntime,
    manager: UpstreamClientManager,
    refs: RefReadWaitsOnce,
    upstream_id: str,
) -> None:
    """Boot the runtime; the admin's Stop lands while boot reads the
    sandbox ref."""
    boot = asyncio.create_task(org_manager.connect_runtime(runtime))
    await asyncio.wait_for(refs.gate.reached.wait(), 5)
    await stop_keeping_sign_ins(
        org_id=ORG,
        upstream_id=upstream_id,
        client_manager=manager,
        connection_store=None,
    )
    assert manager.is_stopped(upstream_id)
    refs.gate.release.set()
    await asyncio.wait_for(boot, 5)


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "never_started"])
async def test_a_stop_during_boot_keeps_the_mcp_stopped(cached: bool) -> None:
    upstream = make_upstream_definition(id="stdio-mcp", command="npx")
    refs = await make_refs(upstream.id, cached=cached)
    fake = make_fake_sandbox_service()
    org_manager, runtime, manager = make_boot(upstream, refs, fake)
    try:
        await boot_with_a_stop_meanwhile(
            org_manager, runtime, manager, refs, upstream.id,
        )

        state = manager.get_state(upstream.id)
        assert manager.is_stopped(upstream.id), (
            "boot overwrote the admin's Stop: the upstream is now "
            f"{state.state if state else None}"
        )
        assert not manager.is_connected(upstream.id)
    finally:
        refs.gate.release.set()
        await manager.stop_all()


async def test_a_tool_call_after_that_boot_opens_no_sandbox() -> None:
    """The consequence the Stop must prevent: the next tool call opening
    a sandbox for an MCP the admin stopped."""
    upstream = make_upstream_definition(id="stdio-mcp", command="npx")
    refs = await make_refs(upstream.id, cached=True)
    fake = make_fake_sandbox_service()
    org_manager, runtime, manager = make_boot(upstream, refs, fake)
    try:
        await boot_with_a_stop_meanwhile(
            org_manager, runtime, manager, refs, upstream.id,
        )
        opened_before = fake.session_open_count

        with pytest.raises(UpstreamStopped):
            await manager.ensure_shared_connected(upstream)

        assert fake.session_open_count == opened_before
    finally:
        refs.gate.release.set()
        await manager.stop_all()
