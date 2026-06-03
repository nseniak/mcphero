"""End-to-end STRESS test for the E2B post-reattach stdout-stall recovery.

Background: reattaching to a paused→resumed E2B sandbox via
``commands.connect`` produces a stdout stream that intermittently
delivers the first response or two and then goes silent — so a tool
refresh right after a reattach used to return a partial catalogue
(tools but no resources/prompts) or hang 30s. See the web research on
e2b-dev issues #1128 / #857 / #1031.

The fix: ``refresh_upstream`` raises a transport stall instead of
persisting a half-empty catalogue, and ``acquire_and_refresh_with_recovery``
(the exact path the dashboard refresh endpoint uses) reconnects on a
FRESH session and retries.

This stress test drives MANY reattach cycles against a live sandbox —
each cycle explicitly pauses the sandbox to force a ``connect_command``
reattach, where the stall occurs ~50-75% of the time — and asserts that
EVERY refresh returns a COMPLETE catalogue (tools AND resources AND
templates AND prompts non-empty). Without the recovery the stalled
cycles would come back partial; with it they all complete. The number
of recoveries actually triggered is logged so a run shows the stall was
exercised.

Skips when ``E2B_API_KEY`` is unset, like the sibling e2e modules.
One sandbox + a fresh create per recovered stall; ~2-4 min, a few cents.

    cd runner/e2b-templates && make build      # one-time
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh \
        tests/integration/test_e2b_reattach_stall_recovery_e2e.py -v -s
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService, RealE2BClient
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    acquire_and_refresh_with_recovery,
)
from tests.unit.factories import make_upstream_definition

E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]
CYCLES: int = int(os.environ.get("STRESS_CYCLES", "6"))

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK reattach-stall stress test skipped",
)

_SERVER_URL = "http://localhost:8000"
_INSTANCE = f"e2e-stall-{TEST_RUN_ID}"


def make_e2b_manager(
    upstream: object, org_id: str,
) -> UpstreamClientManager:
    """Wire the manager exactly as prod does for the E2B reattach path:
    reuse-on-restart + persistence, so the recovery's ref-invalidation
    (forcing a fresh create instead of reattaching to the flaky sandbox)
    is actually exercised."""
    assert E2B_API_KEY is not None
    service = E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=_INSTANCE,
        on_timeout_seconds=120,
        persistence=InMemorySandboxPersistenceRepository(),
        reuse_sandboxes_on_restart=True,
    )
    return UpstreamClientManager(
        upstreams=[upstream],  # type: ignore[list-item]
        org_id=org_id,
        sandbox_services={"e2b": service},
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
    )


def is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


async def _pause_live_sandbox(
    service: E2BSandboxService, manager: UpstreamClientManager, upstream_id: str,
) -> bool:
    """Pause the currently-live sandbox to force a connect_command
    reattach on the next request. Returns False if no live handle (e.g.
    a recovery is mid-flight)."""
    state = manager.get_state(upstream_id)
    task = state.shared_task if state is not None else None
    session_id = getattr(task, "_session_id", None)
    if session_id is None:
        return False
    handle = service._live_sandboxes.get(session_id)  # type: ignore[reportPrivateUsage]
    if handle is None:
        return False
    await handle.pause()
    return True


@pytest.mark.asyncio
async def test_refresh_recovers_from_reattach_stall_under_stress() -> None:
    org_id = f"acme-{TEST_RUN_ID}"
    upstream = make_upstream_definition(
        id=f"e2e-stall-{TEST_RUN_ID}", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    manager = make_e2b_manager(upstream, org_id)
    registry = ToolRegistry([upstream], manager)
    service: Any = manager._sandbox_services["e2b"]  # type: ignore[reportPrivateUsage]

    # Count how many cycles actually triggered the fresh-reconnect
    # recovery, so the run shows the stall was exercised.
    recoveries = [0]
    orig_fresh = manager.reconnect_shared_fresh

    async def _counting_fresh(up: Any, *a: Any, **k: Any) -> None:
        recoveries[0] += 1
        await orig_fresh(up, *a, **k)

    manager.reconnect_shared_fresh = _counting_fresh  # type: ignore[method-assign]

    try:
        # Initial connect + baseline refresh.
        await manager.connect_shared(upstream)
        await acquire_and_refresh_with_recovery(
            org_id=org_id, upstream=upstream, effective_user="",
            connection_store=None, client_manager=manager,
            tool_registry=registry, server_url=_SERVER_URL,
        )

        for cycle in range(1, CYCLES + 1):
            paused = await _pause_live_sandbox(service, manager, upstream.id)
            await acquire_and_refresh_with_recovery(
                org_id=org_id, upstream=upstream, effective_user="",
                connection_store=None, client_manager=manager,
                tool_registry=registry, server_url=_SERVER_URL,
            )
            ids = [upstream.id]
            n_tools = len(registry.get_tools_for_upstreams(ids))
            n_resources = len(registry.get_resources_for_upstreams(ids))
            n_templates = len(registry.get_resource_templates_for_upstreams(ids))
            n_prompts = len(registry.get_prompts_for_upstreams(ids))
            print(
                f"cycle {cycle}: paused={paused} tools={n_tools} "
                f"resources={n_resources} templates={n_templates} "
                f"prompts={n_prompts} recoveries={recoveries[0]}"
            )
            # The contract: every refresh yields a COMPLETE catalogue.
            # Pre-fix, a stalled reattach left resources/templates/prompts
            # empty (or hung); the recovery must heal that every time.
            assert n_tools > 0, f"cycle {cycle}: tools went empty"
            assert n_resources > 0, f"cycle {cycle}: resources went empty"
            assert n_templates > 0, f"cycle {cycle}: templates went empty"
            assert n_prompts > 0, f"cycle {cycle}: prompts went empty"

        print(f"DONE: {recoveries[0]} recovery(ies) over {CYCLES} cycles")
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active account — "
                "run `cd runner/e2b-templates && make build`.",
            )
        raise
    finally:
        try:
            await manager.disconnect_upstream(upstream.id)
        except Exception:
            pass
