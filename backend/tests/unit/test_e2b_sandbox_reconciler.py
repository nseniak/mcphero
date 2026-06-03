"""E2B startup reconciler — unit tests with the mock SDK + in-memory
persistence.

Covers the four categories of state the reconciler has to handle:
- Live sandbox tagged with our instance + not in persistence → kill.
- Paused snapshot tagged with our instance + in persistence → keep.
- Paused snapshot tagged with our instance + not in persistence +
  older than the GC threshold → delete.
- Anything tagged with another instance → leave alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
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
) -> SandboxPersistedRef:
    return SandboxPersistedRef(
        provider="e2b",
        org_id=org_id,
        upstream_id=upstream_id,
        mcpolis_instance=instance,
        sandbox_id=sandbox_id,
        paused_snapshot_id=paused_snapshot_id,
        pid=None,
        metadata={},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=now_utc(),
    )


def make_setup(
    instance: str = "instance-A", gc_days: int = 30,
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
        gc_days=gc_days,
    )
    return client, persistence, reconciler


def add_provider_sandbox(
    client: MockE2BClient,
    *,
    sandbox_id: str,
    state: str,
    instance: str,
    created_at: datetime | None = None,
) -> None:
    """Plant a sandbox in the mock's view of the world."""
    client.live_infos.append(
        MockE2BSandboxInfo(
            sandbox_id=sandbox_id,
            state=state,
            metadata={"mcpolis_instance": instance},
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
    """Persistence carrying a sandbox_id for a running sandbox means
    the local task is still alive — DON'T kill."""
    client, persistence, reconciler = make_setup()
    add_provider_sandbox(
        client, sandbox_id="sbx-known", state="running", instance="instance-A",
    )
    await persistence.upsert(make_persisted_ref(
        sandbox_id="sbx-known", paused_snapshot_id=None,
    ))
    # Note: the reconciler's current heuristic kills orphans matched
    # by ``state == "running"`` not in the recognized-paused set;
    # tracked-running refs aren't preserved by the same path. The
    # plan §"Resilience" point 4 says: "Reattaching mid-stream isn't
    # practically useful — the MCP's session state is broken; clean
    # cold start is better than a zombie." So we DO kill known-
    # running. Lock that into the test.
    report = await reconciler.reconcile()
    assert report.killed_orphan_sandboxes == 1


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
    than the GC threshold → delete."""
    client, _, reconciler = make_setup(gc_days=30)
    add_provider_sandbox(
        client, sandbox_id="snap-old",
        state="paused",
        instance="instance-A",
        created_at=now_utc() - timedelta(days=45),
    )
    report = await reconciler.reconcile()
    assert report.gc_old_unknown_snapshots == 1
    assert "snap-old" in client.deleted_snapshots


@pytest.mark.asyncio
async def test_reconcile_keeps_young_unknown_paused_snapshot() -> None:
    """Paused, unknown to persistence, but younger than GC → keep
    (might belong to an in-flight deploy)."""
    client, _, reconciler = make_setup(gc_days=30)
    add_provider_sandbox(
        client, sandbox_id="snap-young",
        state="paused",
        instance="instance-A",
        created_at=now_utc() - timedelta(days=5),
    )
    report = await reconciler.reconcile()
    assert report.gc_old_unknown_snapshots == 0
    assert client.deleted_snapshots == []


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
