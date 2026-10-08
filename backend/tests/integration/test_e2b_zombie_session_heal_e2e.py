"""Real-SDK regression for the prod "BrokenResourceError on tool
refresh" bug.

Root cause: a service_account shared session whose sandbox died was
reused as a zombie. ``ensure_shared_connected`` early-returned on
``shared_session is not None`` alone, so the next refresh's first send
hit the dead stream and raised ``anyio.BrokenResourceError`` (and the
one before that hung the full 30s, because the pump surfaced the error
as an Exception object the MCP SDK silently drops).

This drives the EXACT prod path against a live E2B sandbox:

1. connect a service_account server-everything; refresh -> OK.
2. kill the sandbox out from under the live session.
3. refresh again. This races the session's stream watcher, and either
   outcome is correct: the refresh reaches the dead session first and
   fails FAST (no 30s/90s hang), or the watcher has already marked the
   session dead, so ``ensure_shared_connected`` rebuilds it and the
   refresh succeeds. It must NOT hang.
4. after a fast failure, refresh once more: ``ensure_shared_connected``
   now sees the dead transport and reconnects a fresh sandbox.
5. either way, the session ends healed: refused, rebuilt on a fresh
   sandbox, and serving ``tools/list``.

Skips when ``E2B_API_KEY`` is unset, like the sibling e2e modules.
One sandbox, ~60s of E2B compute (~$0.02).

To run::

    cd runner/e2b-templates && make build      # one-time
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh \
        tests/integration/test_e2b_zombie_session_heal_e2e.py -v -s
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from mcpolis.adapters.sandbox_e2b import E2BSandboxService, RealE2BClient
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    acquire_upstream_session,
)
from tests.integration._e2b_log_capture import events_since
from tests.unit.factories import make_upstream_definition

E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK zombie-heal test skipped",
)

_SERVER_URL = "http://localhost:8000"
_INSTANCE = f"e2e-zombie-{TEST_RUN_ID}"
# A refresh that reaches the dead session must fail within this (the
# pre-fix inert error-surfacing hung the full 30s).
FAIL_FAST_SECONDS = 20.0
# Outer bound on the first refresh after the kill. When the watcher wins
# the race, that refresh IS the heal: a fresh sandbox plus the MCP's
# handshake, which takes seconds; past this it is hanging.
FIRST_REFRESH_DEADLINE_SECONDS = 60.0


def make_e2b_manager(
    upstream: object, org_id: str,
) -> tuple[UpstreamClientManager, RealE2BClient]:
    assert E2B_API_KEY is not None
    client = RealE2BClient(api_key=E2B_API_KEY)
    service = E2BSandboxService(
        client, mcpolis_instance=_INSTANCE, on_timeout_seconds=120,
    )
    manager = UpstreamClientManager(
        upstreams=[upstream],  # type: ignore[list-item]
        org_id=org_id,
        sandbox_services={"e2b": service},
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
    )
    return manager, client


def is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


def logged_since(event: str, *, upstream_id: str, cursor_ns: int) -> bool:
    """Whether ``event`` was logged for ``upstream_id`` since the cursor.
    The capture is shared by every test in the process, hence the id."""
    return any(
        captured.get("upstream_id") == upstream_id
        for captured in events_since(event, cursor_ns)
    )


async def _refresh(manager, registry, upstream, org_id) -> None:
    await acquire_upstream_session(
        org_id=org_id, upstream=upstream, effective_user="",
        connection_store=None, client_manager=manager, server_url=_SERVER_URL,
    )
    await registry.refresh_upstream(upstream.id)


@pytest.mark.asyncio
async def test_refresh_heals_a_dead_shared_session() -> None:
    org_id = f"acme-{TEST_RUN_ID}"
    upstream = make_upstream_definition(
        id=f"e2e-zombie-{TEST_RUN_ID}", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    manager, client = make_e2b_manager(upstream, org_id)
    registry = ToolRegistry([upstream], manager)

    try:
        # 1) Healthy connect + refresh.
        await manager.connect_shared(upstream)
        await _refresh(manager, registry, upstream, org_id)
        assert registry.get_all_tools(), "server-everything should expose tools"

        # 2) Kill the sandbox under the live session.
        kill_cursor = time.monotonic_ns()
        infos = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": _INSTANCE},
        )
        assert infos, "expected a live sandbox to kill"
        for i in infos:
            sb = await client.connect_sandbox(i.sandbox_id)
            await sb.kill()

        # 3) The first refresh after the kill races the session's stream
        #    watcher, and both outcomes are the designed behaviour
        #    (CLAUDE.md, "Waking a paused sandbox never reuses its MCP
        #    process"):
        #    - the refresh wins: ensure_shared_connected still sees a live
        #      transport, the refresh writes to the dead session and must
        #      FAIL FAST, marking the transport dead (step 4 then heals);
        #    - the watcher wins: the killed sandbox ended the session's
        #      output stream, the watcher logged sandbox.e2b.stream_dead
        #      and marked the transport dead (~0.45 s after the kill in
        #      one full paid run), so ensure_shared_connected refuses the
        #      session BEFORE writing anything
        #      (upstream.client.shared_session.dead_reconnecting), rebuilds
        #      it on a fresh sandbox, and the refresh SUCCEEDS.
        #    Run alone the refresh usually wins; under the full suite's
        #    load the watcher did once, and the old ``pytest.raises`` read
        #    that heal as a failure. What must never happen is a hang.
        started = time.monotonic()
        first_refresh_error: Exception | None = None
        try:
            await asyncio.wait_for(
                _refresh(manager, registry, upstream, org_id),
                timeout=FIRST_REFRESH_DEADLINE_SECONDS,
            )
        except Exception as exc:
            first_refresh_error = exc
        elapsed = time.monotonic() - started
        print(
            f"first refresh after the kill: "
            f"{'raised' if first_refresh_error else 'healed'} "
            f"in {elapsed:.1f}s",
        )
        if first_refresh_error is not None:
            assert elapsed < FAIL_FAST_SECONDS, (
                f"refresh on a dead session must fail fast, took "
                f"{elapsed:.1f}s (the pre-fix inert error-surfacing hung the "
                f"full 30s); it raised {first_refresh_error!r}"
            )
            # 4) Next refresh heals: ensure_shared_connected sees the dead
            #    transport and reconnects a fresh sandbox.
            await _refresh(manager, registry, upstream, org_id)

        # 5) Either way the session is healed: the dead session was
        #    refused, not reused, and a fresh sandbox now serves tools.
        assert logged_since(
            "upstream.client.shared_session.dead_reconnecting",
            upstream_id=upstream.id, cursor_ns=kill_cursor,
        ), (
            "the dead shared session must be refused and rebuilt, not "
            "reused (the zombie's next send raises BrokenResourceError)"
        )
        assert logged_since(
            "sandbox.e2b.create", upstream_id=upstream.id, cursor_ns=kill_cursor,
        ), (
            "the heal must reconnect onto a fresh sandbox; the killed one "
            "is gone"
        )
        state = manager.get_state(upstream.id)
        assert state is not None and state.shared_task is not None
        assert state.shared_task.is_transport_alive(), (
            "after healing, the shared session must report a live transport"
        )
        assert registry.get_all_tools(), (
            "refresh after a dead session must reconnect and return tools, "
            "not reuse the zombie (BrokenResourceError)"
        )
        healed = await asyncio.wait_for(
            manager.get_session(upstream.id).list_tools(), timeout=30.0,
        )
        assert healed.tools, (
            "the healed session must list tools itself; the registry may "
            "still hold the ones listed before the kill"
        )
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
