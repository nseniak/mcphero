"""Pin the boot-skip wiring inside ``OrgRuntimeManager.connect_runtime``.

The "no wakeup at restart" contract relies on a single helper —
``UpstreamClientManager.connect_shared_or_defer`` — being called by
every boot-time entry point. Earlier versions had the gate sitting
in ``UpstreamClientManager.start_all`` (a dev-stack-only path) while
prod boot went through ``OrgRuntimeManager.connect_runtime``,
bypassing the gate entirely. The integration test caught the
behavioral assertion ("no E2B calls at boot") only after the bug
shipped to prod, because the test exercised ``start_all`` and not
``connect_runtime``.

These tests pin the inverse: regardless of test infrastructure
maturity, ``connect_runtime`` MUST funnel through
``connect_shared_or_defer`` so the gate is impossible to bypass.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamSelfDescription,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.org_runtime import (
    OrgRuntime,
    OrgRuntimeManager,
    StartupStatus,
)
from tests.unit.factories import make_upstream_definition


def _seed_cached_ref(
    persistence: InMemorySandboxPersistenceRepository,
    org_id: str, upstream_id: str,
) -> SandboxPersistedRef:
    ref = SandboxPersistedRef(
        provider="e2b",
        org_id=org_id,
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
    return ref


def _make_runtime_with_real_manager(
    *, org_id: str,
    persistence: InMemorySandboxPersistenceRepository | None = None,
    upstreams: list[object] | None = None,
):  # type: ignore[no-untyped-def]
    """Build an OrgRuntime with a REAL ``UpstreamClientManager`` and
    mocked sibling services so ``connect_runtime`` can drive the
    real boot-skip gate without standing up the full app graph.
    """
    persistence = persistence or InMemorySandboxPersistenceRepository()
    upstream_list: list[object] = upstreams or []
    client_manager = UpstreamClientManager(
        upstreams=upstream_list,  # type: ignore[arg-type]
        org_id=org_id,
        sandbox_persistence=persistence,
    )
    # connect_shared is what we need to assert against — make it an
    # AsyncMock so we can verify it's NEVER awaited on the deferred
    # path and DOES await on the eager path. ``connect_shared_or_defer``
    # invokes it for real (so the helper's own logic runs) but the
    # method itself becomes a no-op, which is fine for a unit test.
    client_manager.connect_shared = AsyncMock()  # type: ignore[method-assign]

    tool_registry = MagicMock()
    tool_registry.hydrate = AsyncMock()
    tool_registry.refresh_all = AsyncMock()

    runtime = OrgRuntime(
        org_id=org_id,
        policy_engine=MagicMock(get_admin_emails=MagicMock(return_value=[])),
        tool_registry=tool_registry,
        client_manager=client_manager,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=upstream_list,  # type: ignore[arg-type]
    )
    return runtime, client_manager


def _make_org_manager() -> OrgRuntimeManager:
    """``OrgRuntimeManager`` with all repos mocked. ``connect_runtime``
    only needs ``connection_repo.get_disabled_ids`` to return an
    empty set; the rest is unused for service_account stdio."""
    connection_repo = MagicMock()
    connection_repo.get_disabled_ids = AsyncMock(return_value=set())
    connection_repo.set_disabled = AsyncMock()
    return OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=connection_repo,
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )


@pytest.mark.asyncio
async def test_connect_runtime_skips_connect_shared_when_cache_present() -> None:
    """The bug this test pins: ``connect_runtime`` MUST go through
    ``connect_shared_or_defer`` so a service-account stdio upstream
    with cached metadata in persistence skips ``connect_shared``
    entirely. Earlier code called ``connect_shared`` directly,
    bypassing the gate."""
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")

    persistence = InMemorySandboxPersistenceRepository()
    await persistence.upsert(_seed_cached_ref(persistence, org_id, upstream.id))

    runtime, client_manager = _make_runtime_with_real_manager(
        org_id=org_id, persistence=persistence, upstreams=[upstream],
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)

    connect_shared_mock: AsyncMock = client_manager.connect_shared  # type: ignore[assignment]
    connect_shared_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_runtime_marks_cached_upstream_as_ready_in_ui() -> None:
    """USER-VISIBLE contract: after ``connect_runtime`` boots a
    cached stdio upstream into deferred-attach state, the dashboard
    must show it as ready. The dashboard's readiness gate reads
    ``is_connected(upstream_id)`` from the manager.

    Pins the bug observed in prod after the gate moved into
    ``connect_runtime``: the boot path called ``try_defer_boot_attach``
    directly, bypassing ``connect_shared_or_defer`` — and the latter
    was the *only* place that populated
    ``_deferred_attach_upstreams``. Result: cached upstreams the
    user never stopped showed as "Stopped" in the UI.

    Different abstraction level from the unit tests on
    ``connect_shared_or_defer`` directly (which keep passing because
    that helper still populates the set itself) — those tests at
    the helper level let this regression slip past. The contract
    that matters is ``connect_runtime`` → ``is_connected`` returning
    True, and that's what we assert here.
    """
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence = InMemorySandboxPersistenceRepository()
    await persistence.upsert(_seed_cached_ref(persistence, org_id, upstream.id))

    runtime, client_manager = _make_runtime_with_real_manager(
        org_id=org_id, persistence=persistence, upstreams=[upstream],
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)

    assert client_manager.is_connected(upstream.id), (
        "cached upstream should be reported as connected after "
        "connect_runtime — the dashboard's ``ready`` pill reads "
        "from this gate. Was the deferred-attach state populated?"
    )
    # And the parallel ``ready_upstream_ids`` accessor (used by the
    # superadmin counter / org listings) must also include it.
    assert upstream.id in client_manager.ready_upstream_ids


@pytest.mark.asyncio
async def test_connect_runtime_skips_stdio_when_no_cache() -> None:
    """The ``was_ready`` rule for stdio: with NO cached metadata
    in the persistence ref (= MCP was never successfully running),
    ``connect_runtime`` MUST NOT call ``connect_shared`` at all.
    The earlier "eager-connect-then-auto-disable" path was a
    band-aid — it stopped retrying on the *next* boot but still
    wasted a sandbox spawn / wake on the *current* one.

    Demonstrates the fix for the prod bogus pattern: stdio-no-cache
    is skipped up front, no E2B-side activity at boot.
    """
    from mcpolis.domain.model.upstream import TransportType
    org_id = "acme"
    upstream = make_upstream_definition(
        id="uncached-stdio", transport=TransportType.stdio,
    )

    runtime, client_manager = _make_runtime_with_real_manager(
        org_id=org_id, upstreams=[upstream],
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)

    connect_shared_mock: AsyncMock = client_manager.connect_shared  # type: ignore[assignment]
    connect_shared_mock.assert_not_awaited()
    # And the gate's ``try_defer_boot_attach`` was the *only* call
    # touching the manager's persistence — no auto-disable, no
    # status.failed mutation.
    set_disabled_mock: AsyncMock = (
        org_manager._connection_repo.set_disabled  # pyright: ignore[reportPrivateUsage]
    )  # type: ignore[assignment]
    set_disabled_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_runtime_disables_http_upstream_on_connect_failure() -> None:
    """Auto-disable circuit breaker: service-account HTTP eagerly
    retries every boot (cheap — no sandbox), so a permanently
    broken HTTP MCP would keep failing forever. After the first
    failure, ``set_disabled`` writes an explicit ``enabled: False``
    so the next boot's ``disabled_ids`` includes the upstream and
    the reconciler skips it. Admin can click Reconnect to retry.

    (Stdio takes the upfront-skip path via the cache gate; only
    HTTP exercises the auto-disable branch now.)
    """
    from mcpolis.domain.model.upstream import TransportType
    org_id = "acme"
    upstream = make_upstream_definition(
        id="broken-http",
        transport=TransportType.streamable_http,
        url="http://broken.example/mcp",
    )

    runtime, client_manager = _make_runtime_with_real_manager(
        org_id=org_id, upstreams=[upstream],
    )
    # Make connect_shared raise — simulates a 5xx / DNS failure on
    # the HTTP MCP endpoint.
    client_manager.connect_shared = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("connect timed out"),
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)

    set_disabled_mock: AsyncMock = (
        org_manager._connection_repo.set_disabled  # pyright: ignore[reportPrivateUsage]
    )  # type: ignore[assignment]
    set_disabled_mock.assert_awaited_once_with(org_id, upstream.id)


@pytest.mark.asyncio
async def test_connect_runtime_disable_on_failure_transitions_in_memory_state() -> None:
    """Sibling assertion to ``test_connect_runtime_disables_http_upstream_on_connect_failure``:
    the auto-disable path is a *compound* state mutation — explicit
    ``enabled: False`` to persistence (asserted by the sibling test)
    AND ``transition_to_disabled`` on the in-memory state machine
    (asserted here). Without the in-memory transition the dashboard
    renders the broken upstream as FAILED instead of DISABLED, which
    the user reads as "still being retried" rather than "stopped
    until you click Reconnect."
    """
    from mcpolis.adapters.upstream_clients.upstream_state import (
        UpstreamConnectionState,
    )
    from mcpolis.domain.model.upstream import TransportType
    org_id = "acme"
    upstream = make_upstream_definition(
        id="broken-http-state",
        transport=TransportType.streamable_http,
        url="http://broken.example/mcp",
    )
    runtime, client_manager = _make_runtime_with_real_manager(
        org_id=org_id, upstreams=[upstream],
    )
    client_manager.connect_shared = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("connect timed out"),
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)

    state = client_manager.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED, (
        f"expected DISABLED after auto-disable; got {state.state!r}"
    )
    assert state.last_failure == "connect timed out", (
        "the original exception message must reach the dashboard so "
        "the operator sees WHY the upstream auto-disabled — without "
        "this they only see the binary Stopped pill"
    )


@pytest.mark.asyncio
async def test_connect_runtime_does_not_disable_on_deferred_success() -> None:
    """Negative assertion: a successful deferred-attach must NOT
    call ``set_disabled``. The auto-disable-on-failure path is for
    real failures only — a regression that disabled cached
    upstreams on the success path would silently strand them as
    "Stopped" in the UI.
    """
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence = InMemorySandboxPersistenceRepository()
    await persistence.upsert(_seed_cached_ref(persistence, org_id, upstream.id))

    runtime, _ = _make_runtime_with_real_manager(
        org_id=org_id, persistence=persistence, upstreams=[upstream],
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)

    set_disabled_mock: AsyncMock = (
        org_manager._connection_repo.set_disabled  # pyright: ignore[reportPrivateUsage]
    )  # type: ignore[assignment]
    set_disabled_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_runtime_skips_http_after_prior_failure_set_disabled() -> None:
    """Migration sequence (HTTP path): simulate a permanently
    broken HTTP service-account upstream across two boots. First
    boot fails → ``set_disabled`` fires → next boot's
    ``get_disabled_ids`` includes the upstream → reconciler skips
    it without calling ``connect_shared``.

    Stdio takes the upfront-skip path via the cache gate (see
    ``test_connect_runtime_skips_stdio_when_no_cache``); HTTP is
    the only path that exercises the auto-disable circuit breaker,
    because HTTP has no per-upstream cache to gate on.
    """
    from mcpolis.domain.model.upstream import TransportType
    org_id = "acme"
    upstream = make_upstream_definition(
        id="broken-http-then-skipped",
        transport=TransportType.streamable_http,
        url="http://broken.example/mcp",
    )

    runtime, client_manager = _make_runtime_with_real_manager(
        org_id=org_id, upstreams=[upstream],
    )
    # Failed connect on the first call.
    client_manager.connect_shared = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("connect timed out"),
    )

    # Connection-repo mock with stateful disabled tracking — the
    # set_disabled call from boot 1 mutates the get_disabled_ids
    # response that boot 2 reads, mirroring how prod Mongo / file
    # storage round-trips the explicit False.
    connection_repo = MagicMock()
    disabled_state: set[str] = set()

    async def _get_disabled_ids(_org_id: str) -> set[str]:
        return set(disabled_state)

    async def _set_disabled(_org_id: str, upstream_id: str) -> None:
        disabled_state.add(upstream_id)

    connection_repo.get_disabled_ids = AsyncMock(side_effect=_get_disabled_ids)
    connection_repo.set_disabled = AsyncMock(side_effect=_set_disabled)

    org_manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=connection_repo,
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]

    # Boot 1: connect fails → set_disabled fires.
    await org_manager.connect_runtime(runtime)
    assert upstream.id in disabled_state, (
        f"boot 1 should have called set_disabled; "
        f"disabled_state={disabled_state!r}"
    )
    connect_shared_after_boot_1 = client_manager.connect_shared.await_count

    # Boot 2: get_disabled_ids now returns the upstream → skipped.
    await org_manager.connect_runtime(runtime)
    assert client_manager.connect_shared.await_count == connect_shared_after_boot_1, (
        "boot 2 should have skipped the disabled upstream entirely; "
        f"connect_shared was called {client_manager.connect_shared.await_count} "
        f"times total (was {connect_shared_after_boot_1} after boot 1)"
    )


@pytest.mark.asyncio
async def test_connect_runtime_marks_deferred_upstream_as_connected() -> None:
    """A deferred-attach upstream is "ready" from the user's POV —
    the cache satisfies dashboard reads and the lazy-attach fires
    on first tool call. ``connect_runtime`` must add it to
    ``status.connected`` (not ``status.failed``) so the dashboard's
    ready badge flips green at boot.
    """
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")

    persistence = InMemorySandboxPersistenceRepository()
    await persistence.upsert(_seed_cached_ref(persistence, org_id, upstream.id))

    runtime, _ = _make_runtime_with_real_manager(
        org_id=org_id, persistence=persistence, upstreams=[upstream],
    )
    org_manager = _make_org_manager()
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]
    # Seed the startup-status entry so ``connect_runtime``'s
    # ``status.connected.add(...)`` lands somewhere we can read back.
    # In prod, ``_build_runtime`` populates this; the test-side
    # construction here doesn't go through that path.
    status = StartupStatus(total=1)
    org_manager._startup_status[org_id] = status  # pyright: ignore[reportPrivateUsage]

    await org_manager.connect_runtime(runtime)
    assert upstream.id in status.connected, (
        "deferred-attach upstream should be in status.connected; "
        f"got connected={status.connected!r}, failed={status.failed!r}"
    )
    assert upstream.id not in status.failed
