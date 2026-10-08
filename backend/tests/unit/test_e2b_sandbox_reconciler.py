"""E2B startup reconciler — unit tests with the mock SDK + in-memory
persistence.

Covers the categories of state the reconciler has to handle:
- Sandbox tagged with our instance + a ref points at it → keep,
  running or paused.
- Running sandbox tagged with our instance + no ref → kill, whatever
  else the store holds for its upstream.
- Paused sandbox tagged with our instance + no ref + older than the
  grace → kill; younger → keep.
- Anything tagged with another instance → leave alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.service import (
    SANDBOXES_TO_KILL_METADATA_KEY,
    VOLUME_METADATA_KEY,
    VOLUMES_TO_DESTROY_METADATA_KEY,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.sandbox_reconciler import (
    DEFAULT_PAUSED_ORPHAN_GRACE,
)
from tests.unit.sandbox_e2b_mock import (
    MockE2BClient,
    MockE2BSandboxInfo,
    make_mock_e2b_client,
)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def make_persisted_ref(
    *,
    org_id: str = "acme",
    upstream_id: str = "ups-1",
    paused_snapshot_id: str | None = None,
    sandbox_id: str | None = None,
    instance: str = "instance-A",
    metadata: dict[str, str] | None = None,
) -> SandboxPersistedRef:
    return SandboxPersistedRef(
        provider="e2b",
        org_id=org_id,
        upstream_id=upstream_id,
        mcpolis_instance=instance,
        sandbox_id=sandbox_id,
        paused_snapshot_id=paused_snapshot_id,
        pid=None,
        metadata=metadata or {},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=now_utc(),
    )


def make_setup(
    instance: str = "instance-A",
    paused_orphan_grace: timedelta = DEFAULT_PAUSED_ORPHAN_GRACE,
) -> tuple[
    MockE2BClient,
    InMemorySandboxPersistenceRepository,
    E2BSandboxReconciler,
]:
    client = make_mock_e2b_client()
    persistence = InMemorySandboxPersistenceRepository()
    reconciler = E2BSandboxReconciler(
        client, persistence,
        mcpolis_instance=instance,
        paused_orphan_grace=paused_orphan_grace,
    )
    return client, persistence, reconciler


def add_provider_sandbox(
    client: MockE2BClient,
    *,
    sandbox_id: str,
    state: str,
    instance: str,
    created_at: datetime | None = None,
    org_id: str | None = None,
    upstream_id: str | None = None,
) -> None:
    """Plant a sandbox in the mock's view of the world. ``org_id`` and
    ``upstream_id`` add the attribution tags a real create sets."""
    metadata = {"mcpolis_instance": instance}
    if org_id is not None:
        metadata["mcpolis_org"] = org_id
    if upstream_id is not None:
        metadata["mcpolis_upstream"] = upstream_id
    client.live_infos.append(
        MockE2BSandboxInfo(
            sandbox_id=sandbox_id,
            state=state,
            metadata=metadata,
            created_at=created_at,
        ),
    )


# ---------- constructor invariants ----------


def test_reconciler_rejects_empty_instance() -> None:
    """Without a non-empty mcpolis_instance the reconciler can't tell
    its own sandboxes from another instance's — refuse to construct."""
    from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler

    client = make_mock_e2b_client()
    persistence = InMemorySandboxPersistenceRepository()
    with pytest.raises(ValueError):
        E2BSandboxReconciler(
            client, persistence, mcpolis_instance="",
        )


# ---------- empty / no-op ----------


@pytest.mark.asyncio
async def test_reconcile_no_sandboxes_is_noop() -> None:
    _, _, reconciler = make_setup()
    report = await reconciler.reconcile()
    assert report.killed_orphan_sandboxes == 0
    assert report.kept_paused_snapshots == 0
    assert report.gc_old_unknown_snapshots == 0
    assert report.skipped_other_instance == 0


# ---------- orphan kill ----------


@pytest.mark.asyncio
async def test_reconcile_kills_orphan_running_sandbox() -> None:
    """A live sandbox tagged with our instance that's not in
    persistence is an orphan — kill it."""
    client, _, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-orphan", state="running", instance="instance-A",
    )
    report = await reconciler.reconcile()
    assert report.killed_orphan_sandboxes == 1
    assert any(k.sandbox_id == "sbx-orphan" for k in client.kills)


@pytest.mark.asyncio
async def test_reconcile_keeps_running_sandboxes_we_track() -> None:
    """A running sandbox a ref points at was preserved across the last
    shutdown (or is a live peer's session). The next connect reuses it
    with a fresh MCP process; killing it would throw away its package
    cache on every deploy."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-known", state="running", instance="instance-A",
    )
    await persistence.upsert(make_persisted_ref(
        sandbox_id="sbx-known", paused_snapshot_id=None,
    ))
    report = await reconciler.reconcile()
    assert report.killed_orphan_sandboxes == 0
    assert report.kept_tracked_sandboxes == 1
    assert client.kills == []


@pytest.mark.asyncio
async def test_reconcile_keeps_idle_paused_sandbox_known_by_sandbox_id() -> None:
    """E2B's own idle pause keeps the sandbox id, so the ref names it in
    ``sandbox_id``, not ``paused_snapshot_id``. Old age must not GC it."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-idle", state="paused", instance="instance-A",
        created_at=now_utc() - timedelta(days=90),
    )
    await persistence.upsert(make_persisted_ref(sandbox_id="sbx-idle"))
    report = await reconciler.reconcile()
    assert report.kept_paused_snapshots == 1
    assert report.gc_old_unknown_snapshots == 0
    assert client.kills == []


# ---------- paused snapshot retention ----------


@pytest.mark.asyncio
async def test_reconcile_keeps_recognized_paused_snapshot() -> None:
    """Paused + tagged with our instance + persistence has a matching
    paused_snapshot_id → keep."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="snap-keep", state="paused", instance="instance-A",
    )
    await persistence.upsert(make_persisted_ref(
        paused_snapshot_id="snap-keep",
    ))
    report = await reconciler.reconcile()
    assert report.kept_paused_snapshots == 1
    assert report.gc_old_unknown_snapshots == 0
    # Persistence still carries the ref.
    persisted = await persistence.get(org_id="acme", upstream_id="ups-1")
    assert persisted is not None
    assert persisted.paused_snapshot_id == "snap-keep"


# ---------- GC of old unknown snapshots ----------


@pytest.mark.asyncio
async def test_reconcile_gcs_old_unknown_paused_snapshot() -> None:
    """Paused + tagged with our instance + NOT in persistence + older
    than the grace → delete."""
    client, _, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="snap-old",
        state="paused",
        instance="instance-A",
        created_at=now_utc() - timedelta(days=45),
    )
    report = await reconciler.reconcile()
    assert report.gc_old_unknown_snapshots == 1
    # Removed by a kill: the SDK's delete_snapshot targets templates.
    assert [k.sandbox_id for k in client.kills] == ["snap-old"]


@pytest.mark.asyncio
async def test_reconcile_kills_an_unknown_paused_sandbox_after_hours_not_a_month() -> None:
    """Nearly every orphan is paused by the time a boot sees it (E2B
    pauses an unused sandbox within minutes). Two hours old, no ref:
    killed. The grace used to be 30 days, so orphans lived a month."""
    client, _, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-paused-orphan",
        state="paused",
        instance="instance-A",
        created_at=now_utc() - timedelta(hours=2),
    )
    report = await reconciler.reconcile()
    assert report.gc_old_unknown_snapshots == 1
    assert [k.sandbox_id for k in client.kills] == ["sbx-paused-orphan"]


@pytest.mark.asyncio
async def test_reconcile_keeps_young_unknown_paused_snapshot() -> None:
    """Paused, unknown to persistence, but younger than the grace →
    keep until a later boot."""
    client, _, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="snap-young",
        state="paused",
        instance="instance-A",
        created_at=now_utc() - timedelta(minutes=5),
    )
    report = await reconciler.reconcile()
    assert report.gc_old_unknown_snapshots == 0
    assert client.kills == []


# ---------- what the store holds besides refs ----------


@pytest.mark.asyncio
async def test_reconcile_kills_a_sandbox_whose_start_left_only_a_marker() -> None:
    """Older code wrote a "creating" record (no sandbox id) before each
    create, and the reconcile spared any running sandbox of that
    upstream. At boot the start that wrote it is gone, so the record
    kept that process's sandbox alive for good. Databases still hold
    such records: they must not protect anything."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-dead-start", state="running",
        instance="instance-A", org_id="acme", upstream_id="ups-1",
    )
    await persistence.upsert(make_persisted_ref(upstream_id="ups-1"))

    report = await reconciler.reconcile()

    assert report.killed_orphan_sandboxes == 1
    assert [k.sandbox_id for k in client.kills] == ["sbx-dead-start"]


@pytest.mark.asyncio
async def test_reconcile_kills_the_orphan_of_a_stopped_upstream_with_a_disk() -> None:
    """Stopping an upstream with a persistent disk keeps a ref with only
    the volume id (no sandbox id): the same shape as the old "creating"
    record. If the Stop's kill failed, that ref must not protect the
    sandbox at every later boot."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-stopped-but-alive", state="running",
        instance="instance-A", org_id="acme", upstream_id="ups-vol",
    )
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-vol", metadata={VOLUME_METADATA_KEY: "vol-1"},
    ))

    report = await reconciler.reconcile()

    assert report.killed_orphan_sandboxes == 1
    assert [k.sandbox_id for k in client.kills] == ["sbx-stopped-but-alive"]


# ---------- volumes a removal could not destroy ----------


@pytest.mark.asyncio
async def test_reconcile_destroys_the_volumes_a_removal_could_not() -> None:
    """A removal whose volume destroy failed left only the volume ids on
    the upstream's ref: the boot retries them, and the ref goes once
    nothing is left on it."""
    client, persistence, reconciler = make_setup()
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-gone",
        metadata={VOLUMES_TO_DESTROY_METADATA_KEY: "vol-1 vol-2"},
    ))

    report = await reconciler.reconcile()

    assert [d.volume_id for d in client.volume_destroys] == ["vol-1", "vol-2"]
    assert report.destroyed_leftover_volumes == 2
    assert await persistence.get(org_id="acme", upstream_id="ups-gone") is None


@pytest.mark.asyncio
async def test_reconcile_keeps_a_volume_whose_destroy_fails_again() -> None:
    client, persistence, reconciler = make_setup()
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-gone",
        metadata={VOLUMES_TO_DESTROY_METADATA_KEY: "vol-1"},
    ))
    client.volume_destroy_raises = E2BSDKError("E2BSDKError", "503")

    report = await reconciler.reconcile()

    assert report.destroyed_leftover_volumes == 0
    ref = await persistence.get(org_id="acme", upstream_id="ups-gone")
    assert ref is not None
    assert ref.metadata == {VOLUMES_TO_DESTROY_METADATA_KEY: "vol-1"}


@pytest.mark.asyncio
async def test_reconcile_leaves_the_rest_of_a_re_added_mcps_ref_alone() -> None:
    """The removed MCP's id was added again: the ref is the new MCP's
    too. Only the destroyed volume and the killed sandbox leave it; the
    new MCP's sandbox and volume stay."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-new", state="running", instance="instance-A",
    )
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-again",
        sandbox_id="sbx-new",
        metadata={
            VOLUME_METADATA_KEY: "vol-new",
            VOLUMES_TO_DESTROY_METADATA_KEY: "vol-old",
            SANDBOXES_TO_KILL_METADATA_KEY: "sbx-old",
        },
    ))

    await reconciler.reconcile()

    assert [d.volume_id for d in client.volume_destroys] == ["vol-old"]
    ref = await persistence.get(org_id="acme", upstream_id="ups-again")
    assert ref is not None
    assert ref.sandbox_id == "sbx-new"
    assert ref.metadata == {VOLUME_METADATA_KEY: "vol-new"}
    assert [k.sandbox_id for k in client.kills] == ["sbx-old"]


# ---------- sandboxes a removal could not kill ----------


@pytest.mark.asyncio
async def test_reconcile_kills_the_sandboxes_a_removal_could_not_whatever_their_tag() -> None:
    """A removal whose kill failed left only the sandbox ids on the
    upstream's ref. One carries a tag the listing never returns (made
    before the instance id became one value per database), the other
    is already gone: the boot kills both by id, and the ref goes once
    nothing is left on it."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-pre-upgrade", state="paused",
        instance="per-process-1f3a",
    )
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-gone",
        metadata={SANDBOXES_TO_KILL_METADATA_KEY: "sbx-pre-upgrade sbx-gone"},
    ))

    report = await reconciler.reconcile()

    assert [k.sandbox_id for k in client.kills] == ["sbx-pre-upgrade", "sbx-gone"]
    assert client.live_infos == []
    assert report.killed_leftover_sandboxes == 2
    assert await persistence.get(org_id="acme", upstream_id="ups-gone") is None


@pytest.mark.asyncio
async def test_reconcile_keeps_a_sandbox_whose_kill_fails_again() -> None:
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-pre-upgrade", state="paused",
        instance="per-process-1f3a",
    )
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-gone",
        metadata={SANDBOXES_TO_KILL_METADATA_KEY: "sbx-pre-upgrade"},
    ))
    client.kill_raises = E2BSDKError("E2BSDKError", "503")

    report = await reconciler.reconcile()

    assert report.killed_leftover_sandboxes == 0
    ref = await persistence.get(org_id="acme", upstream_id="ups-gone")
    assert ref is not None
    assert ref.metadata == {SANDBOXES_TO_KILL_METADATA_KEY: "sbx-pre-upgrade"}


# ---------- multi-instance safety ----------


@pytest.mark.asyncio
async def test_reconcile_leaves_other_instance_running_alone() -> None:
    """A live sandbox tagged with a *different* instance is owned by
    that other backend; never touch it."""
    client, _, reconciler = make_setup(instance="instance-A")
    add_provider_sandbox(
        client, sandbox_id="sbx-blue", state="running", instance="instance-B",
    )
    report = await reconciler.reconcile()
    # NOTE: with metadata_filter the mock pre-filters — so the
    # reconciler doesn't see other-instance entries at all. The
    # ``skipped_other_instance`` counter only ticks when the SDK
    # returns extras (defensive double-check). Either way: no kill.
    assert report.killed_orphan_sandboxes == 0
    assert client.kills == []


@pytest.mark.asyncio
async def test_reconcile_skips_other_instance_post_filter() -> None:
    """Defensive double-check: even when the SDK returns sandboxes
    that don't match our filter (e.g. a buggy backend or a
    metadata-not-supported provider), the reconciler refuses to
    touch them."""
    client, _, reconciler = make_setup(instance="instance-A")
    # Manually populate the mock's view with a different-instance
    # entry that bypasses metadata_filter — simulate the SDK leaking.
    client.live_infos.append(
        MockE2BSandboxInfo(
            sandbox_id="sbx-leaked",
            state="running",
            metadata={"mcpolis_instance": "instance-B"},
        ),
    )
    # Force list_sandboxes to ignore the filter and return everything,
    # so the post-filter loop runs.
    real_list = client.list_sandboxes

    async def list_no_filter(*, metadata_filter: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        _: dict[str, str] | None = metadata_filter
        return [info for info in client.live_infos]

    client.list_sandboxes = list_no_filter  # type: ignore[method-assign]
    try:
        report = await reconciler.reconcile()
    finally:
        client.list_sandboxes = real_list  # type: ignore[method-assign]
    assert report.skipped_other_instance == 1
    assert report.killed_orphan_sandboxes == 0


# ---------- failure tolerance ----------


@pytest.mark.asyncio
async def test_reconcile_returns_empty_report_on_list_failure() -> None:
    """A failed list_sandboxes call shouldn't crash the boot path —
    the reconciler logs + returns an empty report so the rest of
    startup can proceed."""
    from mcpolis.adapters.sandbox_e2b.client import E2BSDKError

    client, _, reconciler = make_setup()

    async def explode(**_kwargs: object) -> list[object]:
        raise E2BSDKError("E2BSDKError", "transient")

    client.list_sandboxes = explode  # type: ignore[method-assign]
    report = await reconciler.reconcile()
    assert report.killed_orphan_sandboxes == 0
    assert report.kept_paused_snapshots == 0
    assert report.gc_old_unknown_snapshots == 0


class _UnreadableStore(InMemorySandboxPersistenceRepository):
    """The ref store with Mongo down: listing raises."""

    async def list_all_unscoped(self) -> list[SandboxPersistedRef]:
        raise ConnectionError("mongo unreachable")


@pytest.mark.asyncio
async def test_reconcile_kills_nothing_when_the_refs_cannot_be_read() -> None:
    """Without the refs every sandbox looks like an orphan. A failed
    read must end the pass, not the boot, and must kill nothing."""
    client = make_mock_e2b_client()
    add_provider_sandbox(
        client, sandbox_id="sbx-tracked", state="running",
        instance="instance-A",
    )
    reconciler = E2BSandboxReconciler(
        client, _UnreadableStore(), mcpolis_instance="instance-A",
    )

    report = await reconciler.reconcile()

    assert report.killed_orphan_sandboxes == 0
    assert client.kills == []
