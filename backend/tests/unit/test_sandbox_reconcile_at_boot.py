"""Boot-time wiring of the E2B sandbox reconciler.

Covers ``_run_sandbox_reconcile_at_boot`` — the helper app.py's
lifespan calls between ``initialize_storage`` and traffic
acceptance. Validated via direct invocation rather than the full
FastAPI lifespan so the assertions stay focused on the
preconditions + the SSE event.

Coverage:
- Provider != ``e2b`` → no-op (own-runner / local-subprocess).
- Provider is ``e2b`` but services dict missing the key → no-op.
- In-memory persistence → skip with a structured log signal
  (the plan forbids reconciling against an empty repository).
- Custom (non-mcpolis) E2BSandboxService implementation → no-op.
- Mongo persistence + e2b service registered → reconcile fires
  and a ``sandbox_reconcile_report`` SSE event lands on the
  ``DEFAULT_ORG_ID`` stream.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from mcpolis.adapters.event_stream_inprocess import InProcessEventStream
from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.mongo_sandbox_persistence_repository import (
    MongoSandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_service import (
    SandboxProviderName,
    SandboxService,
)
from mcpolis.entrypoints.app import _run_sandbox_reconcile_at_boot
from mcpolis.entrypoints.config import Settings
from tests.unit.sandbox_e2b_mock import (
    MockE2BClient,
    MockE2BSandboxInfo,
    make_mock_e2b_client,
)


# ---------- helpers ----------


def make_settings(provider: str = "e2b") -> Settings:
    return Settings(  # type: ignore[call-arg]
        sandbox_provider=provider,
    )


def make_storage_stub(
    persistence: SandboxPersistenceRepository,
    event_stream: InProcessEventStream,
) -> Any:
    """Minimal duck-typed StorageBundle. Only the two attributes the
    helper reads need to exist."""

    class _Storage:
        sandbox_persistence_repo = persistence
        # The helper doesn't touch event_stream via storage; it's
        # passed as a separate kwarg. Kept here for parity with the
        # real bundle.

    _ = event_stream
    return _Storage()


def make_e2b_service(
    *, instance: str = "instance-A", client: MockE2BClient | None = None,
) -> tuple[E2BSandboxService, MockE2BClient]:
    real = client or make_mock_e2b_client()
    return E2BSandboxService(real, mcpolis_instance=instance, on_timeout_seconds=60), real


def make_persisted_ref(
    *,
    org_id: str = "acme",
    upstream_id: str = "ups-1",
    sandbox_id: str | None = None,
    paused_snapshot_id: str | None = None,
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
        last_updated=datetime.now(tz=timezone.utc),
    )


import asyncio


def make_wildcard_subscriber(
    event_stream: InProcessEventStream,
) -> "asyncio.Queue[Any]":
    """Plant a wildcard subscriber on the event stream so the test
    can read everything published, regardless of routing key.

    InProcessEventStream's publish() routes by ``user_email``; an
    event with ``user_email=None`` broadcasts to every subscriber.
    The reconcile event sets no user_email (it's operator-level),
    so a single subscribed queue catches it.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
    event_stream._subscribers.setdefault("*", []).append(queue)  # type: ignore[reportPrivateUsage]
    return queue


def drain(queue: "asyncio.Queue[Any]") -> list[Any]:
    out: list[Any] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


# ---------- tests ----------


@pytest.mark.asyncio
async def test_no_op_when_provider_is_not_e2b() -> None:
    """own-runner / local-subprocess deployments don't have a
    reconciler today (own-runner is deferred per docs §9 OPEN);
    helper must return cleanly without touching the persistence
    repo."""
    persistence = MongoSandboxPersistenceRepository.__new__(
        MongoSandboxPersistenceRepository,
    )  # never used because we exit early
    event_stream = InProcessEventStream()
    subscriber = make_wildcard_subscriber(event_stream)
    settings = make_settings(provider="own-runner")
    services: dict[SandboxProviderName, SandboxService] = {}

    await _run_sandbox_reconcile_at_boot(
        settings=settings,
        storage=make_storage_stub(persistence, event_stream),
        sandbox_services=services,
        mcpolis_instance="instance-A",
        event_stream=event_stream,
    )
    # No event published.
    assert drain(subscriber) == []


@pytest.mark.asyncio
async def test_no_op_when_e2b_service_not_registered() -> None:
    """Provider configured as e2b but the service registry is
    missing the key — the validator should have caught this earlier
    but we defend in depth."""
    persistence = MongoSandboxPersistenceRepository.__new__(
        MongoSandboxPersistenceRepository,
    )
    event_stream = InProcessEventStream()
    subscriber = make_wildcard_subscriber(event_stream)
    settings = make_settings(provider="e2b")
    services: dict[SandboxProviderName, SandboxService] = {}

    await _run_sandbox_reconcile_at_boot(
        settings=settings,
        storage=make_storage_stub(persistence, event_stream),
        sandbox_services=services,
        mcpolis_instance="instance-A",
        event_stream=event_stream,
    )
    assert drain(subscriber) == []


@pytest.mark.asyncio
async def test_skip_when_persistence_is_in_memory() -> None:
    """Standalone mode keeps refs in memory. The reconciler would
    treat every live sandbox as an orphan + kill them all — the
    plan explicitly forbids that. The helper must skip silently."""
    service, mock = make_e2b_service()
    persistence = InMemorySandboxPersistenceRepository()
    event_stream = InProcessEventStream()
    subscriber = make_wildcard_subscriber(event_stream)
    settings = make_settings(provider="e2b")

    # Plant a "live" sandbox attributed to our instance to confirm
    # the helper does NOT touch it.
    mock.live_infos.append(
        MockE2BSandboxInfo(
            sandbox_id="sbx-orphan",
            state="running",
            metadata={"mcpolis_instance": "instance-A"},
        ),
    )

    await _run_sandbox_reconcile_at_boot(
        settings=settings,
        storage=make_storage_stub(persistence, event_stream),
        sandbox_services={"e2b": service},
        mcpolis_instance="instance-A",
        event_stream=event_stream,
    )
    # The orphan was NOT killed.
    assert mock.kills == []
    # No event published — the skip is silent (just a structured log
    # which we don't capture in unit tests).
    assert drain(subscriber) == []


@pytest.mark.asyncio
async def test_no_op_when_e2b_service_is_not_E2BSandboxService() -> None:
    """Operator wired a custom impl; let them own its lifecycle.
    The startup helper only reconciles the canonical E2BSandboxService."""

    class _CustomService:
        name: SandboxProviderName = "e2b"

        def capabilities(self) -> Any:
            return None

        def validate_resources(self, _r: Any) -> None: ...

        def session(self, **_kw: Any) -> Any: ...

        async def pause(self, _session_id: str) -> Any:
            return None

        def map_exit(self, _raw: Any) -> Any:
            return None

    persistence = MongoSandboxPersistenceRepository.__new__(
        MongoSandboxPersistenceRepository,
    )
    event_stream = InProcessEventStream()
    subscriber = make_wildcard_subscriber(event_stream)
    settings = make_settings(provider="e2b")
    services: dict[SandboxProviderName, SandboxService] = {
        "e2b": _CustomService(),  # type: ignore[dict-item]
    }

    await _run_sandbox_reconcile_at_boot(
        settings=settings,
        storage=make_storage_stub(persistence, event_stream),
        sandbox_services=services,
        mcpolis_instance="instance-A",
        event_stream=event_stream,
    )
    assert drain(subscriber) == []


# A standalone Mongo-style repo wrapper that satisfies the
# ``isinstance(.., MongoSandboxPersistenceRepository)`` guard inside
# the helper without needing a real Motor connection. The helper's
# only behavioural use of persistence is ``list_all_unscoped``;
# delegate to an in-memory store but report as Mongo.
class _FakeMongoSandboxPersistenceRepository(MongoSandboxPersistenceRepository):
    def __init__(self) -> None:
        # Skip super().__init__ — we never touch the OrgScopedCollection.
        self._inner = InMemorySandboxPersistenceRepository()

    async def upsert(self, ref: SandboxPersistedRef) -> None:
        await self._inner.upsert(ref)

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        return await self._inner.get(org_id=org_id, upstream_id=upstream_id)

    async def delete(self, *, org_id: str, upstream_id: str) -> None:
        await self._inner.delete(org_id=org_id, upstream_id=upstream_id)

    async def list_for_org(
        self, *, org_id: str,
    ) -> list[SandboxPersistedRef]:
        return await self._inner.list_for_org(org_id=org_id)

    async def list_all_unscoped(self) -> list[SandboxPersistedRef]:
        return await self._inner.list_all_unscoped()


@pytest.mark.asyncio
async def test_reconcile_runs_and_publishes_event_under_default_org() -> None:
    """Happy path: ``e2b`` provider + Mongo-shaped persistence + a
    canonical service. The helper kicks reconcile and an SSE event
    lands on the DEFAULT_ORG_ID stream so the admin UI can render
    the operator-visible report."""
    service, mock = make_e2b_service(instance="instance-A")
    persistence = _FakeMongoSandboxPersistenceRepository()
    event_stream = InProcessEventStream()
    subscriber = make_wildcard_subscriber(event_stream)
    settings = make_settings(provider="e2b")

    # Plant: one orphan running + one recognized paused snapshot.
    mock.live_infos.append(
        MockE2BSandboxInfo(
            sandbox_id="sbx-orphan",
            state="running",
            metadata={"mcpolis_instance": "instance-A"},
        ),
    )
    mock.live_infos.append(
        MockE2BSandboxInfo(
            sandbox_id="snap-known",
            state="paused",
            metadata={"mcpolis_instance": "instance-A"},
        ),
    )
    await persistence.upsert(make_persisted_ref(
        upstream_id="ups-known", paused_snapshot_id="snap-known",
    ))

    await _run_sandbox_reconcile_at_boot(
        settings=settings,
        storage=make_storage_stub(persistence, event_stream),
        sandbox_services={"e2b": service},
        mcpolis_instance="instance-A",
        event_stream=event_stream,
    )

    # Orphan got killed.
    assert any(k.sandbox_id == "sbx-orphan" for k in mock.kills)
    # Known paused snapshot survived.
    assert "snap-known" not in mock.deleted_snapshots
    # Event landed on the wildcard subscriber.
    events = drain(subscriber)
    assert len(events) == 1
    received: Any = events[0]
    # ``InProcessEventStream`` queues the Event object directly;
    # pull the typed payload via attribute access.
    assert getattr(received, "type", None) == "sandbox_reconcile_report"
    p: Any = getattr(received, "payload", {})
    assert p["provider"] == "e2b"
    assert p["mcpolis_instance"] == "instance-A"
    assert p["killed_orphan_sandboxes"] == 1
    assert p["kept_paused_snapshots"] == 1
