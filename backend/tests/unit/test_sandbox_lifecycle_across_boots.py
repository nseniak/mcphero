"""Sandboxes across backend restarts: nothing is left that no boot will
ever clean up.

Each test plays several "boots" against one E2B account (one mock
client) and one sandbox-ref store (one database, so one stable instance
id): a boot builds a fresh ``E2BSandboxService`` and runs the boot
reconcile before any session, as the app lifespan does.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler, E2BSandboxService
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.service import (
    SANDBOX_INSTANCE_METADATA_KEY,
    sandboxes_to_kill,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from tests.unit.factories import make_upstream_definition
from tests.unit.sandbox_e2b_mock import (
    MockE2BClient,
    MockE2BSandboxInfo,
    make_mock_e2b_client,
)
from tests.unit.test_e2b_sandbox_service import make_default_resources
from tests.unit.test_sandbox_concurrency import make_choked_create

# The database's instance id: every boot on that database uses it.
STABLE = "stable-db-instance"


def make_boot(
    client: MockE2BClient,
    store: InMemorySandboxPersistenceRepository,
    *,
    instance: str = STABLE,
) -> E2BSandboxService:
    """One backend process, production shape: reuse on, refs stored."""
    return E2BSandboxService(
        client,
        mcpolis_instance=instance,
        on_timeout_seconds=60,
        persistence=store,
        volumes_enabled=False,
        reuse_sandboxes_on_restart=True,
    )


def make_reconciler(
    client: MockE2BClient, store: InMemorySandboxPersistenceRepository,
) -> E2BSandboxReconciler:
    return E2BSandboxReconciler(client, store, mcpolis_instance=STABLE)


def alive_ids(client: MockE2BClient) -> list[str]:
    return [info.sandbox_id for info in client.live_infos]


def make_e2b_pause(
    client: MockE2BClient, sandbox_id: str, *, created_at: datetime,
) -> None:
    """E2B pausing an idle sandbox, as seen at a boot that comes
    ``now - created_at`` after its creation."""
    for info in list(client.live_infos):
        if info.sandbox_id == sandbox_id:
            client.live_infos.remove(info)
            client.live_infos.append(MockE2BSandboxInfo(
                sandbox_id=sandbox_id,
                state="paused",
                metadata=info.metadata,
                created_at=created_at,
            ))


async def open_and_close_preserved(
    service: E2BSandboxService, upstream: UpstreamDefinition, session_id: str,
) -> None:
    """One session that ends in a deploy: the sandbox and its ref stay."""
    async with service.session(
        session_id=session_id,
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        service.mark_session_preserve_on_close(session_id)


async def open_and_hold(
    service: E2BSandboxService, upstream: UpstreamDefinition, session_id: str,
) -> None:
    """A session that stays open until its task is cancelled."""
    async with service.session(
        session_id=session_id,
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_start_cut_by_a_crash_leaves_one_sandbox_after_the_next_boot() -> None:
    """Boot 1 dies (OOM, a kill past the stop grace, a host reboot) while
    a start is creating a sandbox: the sandbox exists on E2B and no ref
    points at it. Boot 2's reconcile, which runs before any start, must
    kill it, so boot 2's first connect leaves the upstream with one
    sandbox. Older code wrote a "creating" record before each create and
    the reconcile spared that upstream's running sandbox, so the dead
    start's sandbox survived every boot next to its replacement."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-crash", command="npx")

    boot_1 = make_boot(client, store)
    gate, arrived = asyncio.Event(), asyncio.Event()
    client.create_sandbox = make_choked_create(  # type: ignore[method-assign]
        client, gate=gate, arrived=arrived,
    )
    dying = asyncio.create_task(open_and_hold(boot_1, upstream, "s1"))
    await asyncio.wait_for(arrived.wait(), timeout=5)
    [orphan] = alive_ids(client)
    del client.create_sandbox  # back to the mock's own create

    try:
        report = await make_reconciler(client, store).reconcile()
        boot_2 = make_boot(client, store)
        await open_and_close_preserved(boot_2, upstream, "s2")

        assert report.killed_orphan_sandboxes == 1, report
        assert orphan not in alive_ids(client), (
            f"the dead start's sandbox {orphan} survived the next boot; "
            f"alive={alive_ids(client)}"
        )
        assert len(alive_ids(client)) == 1, alive_ids(client)
    finally:
        dying.cancel()
        await asyncio.gather(dying, return_exceptions=True)


async def remove_with_a_failing_kill(
    service: E2BSandboxService, client: MockE2BClient, upstream_id: str,
) -> None:
    """An admin removes the upstream while E2B refuses kills: the
    removal's Stop keeps the ref for a retry, then the removal keeps the
    sandbox on it only as one still to kill (an upstream added again
    under the id must never get the removed one's sandbox)."""
    client.kill_raises = E2BSDKError("E2BSDKError", "502 Bad Gateway")
    await service.kill_persisted_session(org_id="acme", upstream_id=upstream_id)
    await service.on_upstream_removed(org_id="acme", upstream_id=upstream_id)
    client.kill_raises = None


async def assert_named_only_to_kill(
    store: InMemorySandboxPersistenceRepository,
    upstream_id: str,
    sandbox_id: str,
) -> None:
    """The ref of ``upstream_id`` names ``sandbox_id`` only as one still
    to kill: never as a sandbox to reuse."""
    ref = await store.get(org_id="acme", upstream_id=upstream_id)
    assert ref is not None, (
        f"nothing names {sandbox_id} any more: no boot will ever kill it"
    )
    assert (ref.sandbox_id, sandboxes_to_kill(ref)) == (None, [sandbox_id])


@pytest.mark.asyncio
async def test_a_sandbox_whose_removal_kill_failed_is_killed_at_the_next_boot() -> None:
    """A removal whose E2B kill failed keeps naming the sandbox, as one
    still to kill, and E2B pauses it within minutes
    (``on_timeout=pause``). The next deploy comes hours later: its
    reconcile kills it, and the ref goes."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-gone", command="npx")
    boot_1 = make_boot(client, store)
    await open_and_close_preserved(boot_1, upstream, "s")
    [sandbox_id] = alive_ids(client)

    await remove_with_a_failing_kill(boot_1, client, "ups-gone")
    await assert_named_only_to_kill(store, "ups-gone", sandbox_id)
    make_e2b_pause(
        client, sandbox_id, created_at=datetime.now(UTC) - timedelta(hours=3),
    )

    report = await make_reconciler(client, store).reconcile()

    assert sandbox_id not in alive_ids(client), (
        f"paused sandbox {sandbox_id} kept by the boot reconcile: {report!r}"
    )
    assert await store.get(org_id="acme", upstream_id="ups-gone") is None


@pytest.mark.asyncio
async def test_a_sandbox_made_before_the_upgrade_whose_removal_kill_fails_is_killed_at_the_next_boot() -> None:
    """The first deploy of the stable instance id: every hosted MCP's
    sandbox still carries its old per-process tag, which the boot
    reconcile does not list. A Stop's kill fails, then the MCP is
    removed and that kill fails too. The ref is all that still names
    the sandbox: deleted, the sandbox stayed (paused) on the account for
    good. The next boot's reconcile kills it by id, whatever its tag."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    await store.upsert(make_sandbox_from_before_the_upgrade(client))

    await remove_with_a_failing_kill(make_boot(client, store), client, "ups-old")
    await assert_named_only_to_kill(store, "ups-old", "sbx-old")
    report = await make_reconciler(client, store).reconcile()

    assert "sbx-old" not in alive_ids(client), (
        f"the removed MCP's pre-upgrade sandbox survived the next boot: {report!r}"
    )
    assert report.killed_leftover_sandboxes == 1, report
    assert await store.get(org_id="acme", upstream_id="ups-old") is None


@pytest.mark.asyncio
async def test_an_mcp_added_again_never_reuses_the_removed_ones_sandbox() -> None:
    """The removal's kill failed, so the removed MCP's sandbox still
    runs. A new MCP added under the same id gets a sandbox of its own,
    its Stop keeps naming the old one as still to kill, and its removal
    kills both."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-again", command="npx")
    service = make_boot(client, store)
    await open_and_close_preserved(service, upstream, "old")
    [old_sandbox] = alive_ids(client)
    await remove_with_a_failing_kill(service, client, "ups-again")

    # Added again: Start, Stop, then removed.
    await open_and_close(service, upstream, "new")
    await assert_named_only_to_kill(store, "ups-again", old_sandbox)
    await service.on_upstream_removed(org_id="acme", upstream_id="ups-again")

    assert client.connects == [], "the removed MCP's sandbox was reused"
    assert alive_ids(client) == [], client.kills
    assert await store.get(org_id="acme", upstream_id="ups-again") is None


@pytest.mark.asyncio
async def test_a_sandbox_the_fresh_sandboxes_override_could_not_kill_is_killed_at_boot() -> None:
    """``MCPOLIS_E2B_FRESH_SANDBOXES`` clears every ref so the MCPs start
    fresh. A kill it could not do keeps its sandbox on the ref as one
    still to kill, never to reuse, and the reconcile right after kills
    it, whatever its tag. Cleared, the ref was the last thing that named
    a sandbox made before the upgrade."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    await store.upsert(make_sandbox_from_before_the_upgrade(client))
    boot = make_boot(client, store)

    client.kill_raises = E2BSDKError("E2BSDKError", "502 Bad Gateway")
    await boot.wipe_for_fresh_restart()
    client.kill_raises = None
    await assert_named_only_to_kill(store, "ups-old", "sbx-old")
    await make_reconciler(client, store).reconcile()

    assert "sbx-old" not in alive_ids(client)
    assert await store.get(org_id="acme", upstream_id="ups-old") is None


@pytest.mark.asyncio
async def test_a_sandbox_made_before_the_upgrade_is_replaced_not_reused() -> None:
    """Before the instance id became one value per database, each process
    tagged its sandboxes with its own id, and refs did not record the
    tag. The reconcile lists sandboxes by the current tag only, so such a
    sandbox is invisible to it: reused, it would leak for good once its
    ref goes. The first connect after the upgrade must kill it and create
    a sandbox carrying the current tag."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    await store.upsert(make_sandbox_from_before_the_upgrade(client))
    upstream = make_upstream_definition(id="ups-old", command="npx")

    await open_and_close_preserved(make_boot(client, store), upstream, "s")

    assert client.connects == [], "a sandbox of another instance was reused"
    assert "sbx-old" not in alive_ids(client), (
        "the sandbox nothing will point at any more must be killed"
    )
    assert [create.metadata["mcpolis_instance"] for create in client.creates] == [
        STABLE,
    ]
    ref = await store.get(org_id="acme", upstream_id="ups-old")
    assert ref is not None and ref.sandbox_id != "sbx-old"
    assert ref.metadata[SANDBOX_INSTANCE_METADATA_KEY] == STABLE


@pytest.mark.asyncio
async def test_every_sandbox_stays_visible_to_the_reconcile_across_the_upgrade() -> None:
    """The last process before the upgrade leaves a sandbox for the next
    boot. After the upgrade, that sandbox is replaced instead of reused,
    so when the replacement later loses its ref (a removal whose kill
    failed), the reconcile still finds it by its tag. Reused, the old
    sandbox kept its old tag forever and no boot could reap it."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-old", command="npx")
    old_process = make_boot(client, store, instance="per-process-1f3a")
    await open_and_close_preserved(old_process, upstream, "old")
    [old_sandbox] = alive_ids(client)

    first_boot = make_boot(client, store)
    await open_and_close_preserved(first_boot, upstream, "new")
    await remove_with_a_failing_kill(first_boot, client, "ups-old")
    await make_reconciler(client, store).reconcile()

    assert alive_ids(client) == [], (
        f"a sandbox outlived its ref and the next reconcile: "
        f"{alive_ids(client)} (the old one was {old_sandbox})"
    )


@pytest.mark.asyncio
async def test_a_sandbox_of_the_same_instance_is_still_reused() -> None:
    """The tag check must not throw away the saving it guards: across a
    restart on the same database the warm sandbox is reused."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-warm", command="npx")
    await open_and_close_preserved(make_boot(client, store), upstream, "a")

    await open_and_close_preserved(make_boot(client, store), upstream, "b")

    assert len(client.creates) == 1, "the warm sandbox was not reused"
    assert client.kills == []


def make_sandbox_from_before_the_upgrade(
    client: MockE2BClient,
) -> SandboxPersistedRef:
    """A sandbox that a process before the upgrade left paused, tagged
    with that process's own id, and the ref naming it (the ref does not
    record the tag)."""
    client.live_infos.append(MockE2BSandboxInfo(
        sandbox_id="sbx-old",
        state="paused",
        metadata={
            "mcpolis_instance": "per-process-1f3a",
            "mcpolis_org": "acme",
            "mcpolis_upstream": "ups-old",
        },
    ))
    return SandboxPersistedRef(
        provider="e2b",
        org_id="acme",
        upstream_id="ups-old",
        mcpolis_instance="per-process-1f3a",
        sandbox_id="sbx-old",
        paused_snapshot_id=None,
        pid=4242,
        metadata={"e2b_template": "mcpolis-node-cpu1-ram1024"},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=datetime.now(UTC),
    )


async def open_and_close(
    service: E2BSandboxService, upstream: UpstreamDefinition, session_id: str,
) -> None:
    """One session that ends in a Stop: its close kills the sandbox."""
    async with service.session(
        session_id=session_id,
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass


@pytest.mark.asyncio
async def test_a_sandbox_made_before_the_upgrade_whose_kill_fails_is_not_lost() -> None:
    """The first connect after the upgrade kills the old sandbox before
    its fresh create overwrites the only ref naming it. When that kill
    fails (an E2B 5xx), the ref must keep naming the old sandbox and the
    fresh one goes unrecorded: overwritten, the old sandbox stayed paused
    on the account for good, since the reconcile lists the current tag
    only. The next connect retries the kill."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    await store.upsert(make_sandbox_from_before_the_upgrade(client))
    upstream = make_upstream_definition(id="ups-old", command="npx")

    client.kill_raises = E2BSDKError("E2BSDKError", "502 Bad Gateway")
    await open_and_close_preserved(make_boot(client, store), upstream, "first")
    client.kill_raises = None
    kept = await store.get(org_id="acme", upstream_id="ups-old")
    # The next boot: its reconcile, then the MCP's next connect.
    await make_reconciler(client, store).reconcile()
    await open_and_close_preserved(make_boot(client, store), upstream, "second")

    assert kept is not None and kept.sandbox_id == "sbx-old"
    ref = await store.get(org_id="acme", upstream_id="ups-old")
    assert ref is not None and alive_ids(client) == [ref.sandbox_id], (
        f"alive={alive_ids(client)}, ref names "
        f"{ref.sandbox_id if ref else None}"
    )
    assert ref.metadata[SANDBOX_INSTANCE_METADATA_KEY] == STABLE


@pytest.mark.asyncio
async def test_a_kill_refused_at_stop_is_retried_by_the_next_stop() -> None:
    """Stop: the close's kill is refused, and so is Stop's second chance
    (``kill_persisted_session``). The ref must still name the sandbox, so
    that a second Stop, or the next boot's (``boot_skip_disabled``), kills
    it. Deleted, the sandbox waited for the next deploy's reconcile."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    service = make_boot(client, store)
    upstream = make_upstream_definition(id="ups-retry", command="npx")

    client.kill_raises = E2BSDKError("E2BSDKError", "502 Bad Gateway")
    await open_and_close(service, upstream, "s")
    await service.kill_persisted_session(org_id="acme", upstream_id="ups-retry")
    client.kill_raises = None
    await service.kill_persisted_session(org_id="acme", upstream_id="ups-retry")

    assert alive_ids(client) == [], client.kills
    assert await store.get(org_id="acme", upstream_id="ups-retry") is None


@pytest.mark.asyncio
async def test_a_kill_refused_at_stop_is_retried_at_the_next_boot() -> None:
    """The same Stop, then a deploy: the reconcile keeps the sandbox (a
    ref points at it) and the boot's ``boot_skip_disabled`` kills it."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-retry", command="npx")
    boot_1 = make_boot(client, store)
    client.kill_raises = E2BSDKError("E2BSDKError", "502 Bad Gateway")
    await open_and_close(boot_1, upstream, "s")
    await boot_1.kill_persisted_session(org_id="acme", upstream_id="ups-retry")
    client.kill_raises = None
    [sandbox_id] = alive_ids(client)
    make_e2b_pause(client, sandbox_id, created_at=datetime.now(UTC))

    report = await make_reconciler(client, store).reconcile()
    await make_boot(client, store).kill_persisted_session(
        org_id="acme", upstream_id="ups-retry",
    )

    assert report.kept_paused_snapshots == 1, report
    assert alive_ids(client) == [], client.kills
    assert await store.get(org_id="acme", upstream_id="ups-retry") is None
