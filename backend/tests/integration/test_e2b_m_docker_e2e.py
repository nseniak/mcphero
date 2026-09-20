"""Broad-matrix real-SDK guardrails: docker + uvx tiers (E2B-M3, M1).

Part of the split E2B broad-matrix suite. These now run in the
standard paid integration leg (``run-integration-tests.sh``,
``make test-all``) whenever ``E2B_API_KEY`` is set and
``NO_INTEGRATION`` is unset — the suite was split across
``test_e2b_m_*_e2e.py`` siblings so ``--dist loadfile`` spreads the
cost across the xdist workers instead of running the whole sweep
serially on one worker.

This file pairs the docker-tier boot (M3 — the slowest non-tiers
test, since it has to boot dind) with the lighter uvx-package test
(M1), so the two together roughly balance against the other split
files. Cost: ~$0.02-0.04 across the file (one docker sandbox + one
python uvx sandbox). Every sandbox is tagged with a per-run UUID and
torn down by ``service.session()`` so a parallel run never sees
another run's sandboxes.
"""
from __future__ import annotations

import asyncio
import contextlib
from io import StringIO
from typing import cast

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.adapters.sandbox_e2b.template_grid import template_name_for
from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from tests.integration._e2b_broad_matrix_helpers import (
    DOCKER_INITIALIZE_TIMEOUT,
    E2B_API_KEY,
    INITIALIZE_TIMEOUT,
    REATTACH_WAIT_SECONDS,
    TEST_RUN_ID,
    TOOL_CALL_TIMEOUT,
    is_template_missing_error,
    make_docker_upstream,
    make_resources,
    make_service,
    make_test_client,
    make_uvx_time_upstream,
)

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="needs a live E2B account (E2B_API_KEY unset)",
)


# ---------------------------------------------------------------------------
# E2B-M1 — uvx real package: initialize + tools + call (python tier)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m1_uvx_real_package_initialize_tools_call_broad_matrix() -> None:
    """E2B-M1: a real uvx package (``mcp-server-time``) cold-installs
    and serves on ``mcpolis-python-cpu1-ram1024``: initialize, list
    tools, then call ``get_current_time(UTC)`` and confirm the arg
    round-trips through the response."""
    instance = f"e2e-m1-{TEST_RUN_ID}"
    service = make_service(instance=instance)
    upstream = make_uvx_time_upstream("m1")
    errlog = StringIO()
    try:
        async with service.session(
            session_id=instance,
            org_id=f"acme-m1-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_resources(1.0, 1024),
            denylist=(),
            errlog=errlog,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                tools = await asyncio.wait_for(
                    session.list_tools(), timeout=TOOL_CALL_TIMEOUT,
                )
                assert any(
                    t.name == "get_current_time" for t in tools.tools
                ), "mcp-server-time should expose get_current_time"
                result = await asyncio.wait_for(
                    session.call_tool(
                        "get_current_time", {"timezone": "UTC"},
                    ),
                    timeout=TOOL_CALL_TIMEOUT,
                )
                blob = " ".join(
                    c.text for c in result.content
                    if getattr(c, "type", None) == "text"
                )
                assert "UTC" in blob or "utc" in blob.lower(), (
                    f"get_current_time(UTC) didn't mention UTC: {blob[:200]!r}"
                )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-python-cpu1-ram1024 not published — run the "
                "template grid build first.",
            )
        tail = errlog.getvalue()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise


# ---------------------------------------------------------------------------
# E2B-M3 — docker tier beyond the floor (cpu4-ram4096): initialize + tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m3_docker_higher_tier_initialize_tools_broad_matrix() -> None:
    """E2B-M3: a docker MCP on a tier ABOVE the cpu2-ram2048 floor
    (``mcpolis-docker-cpu4-ram4096``). Boots dind, runs
    ``docker run -i --rm mcp/everything``, MCP-initializes, lists
    tools. The targeted docker e2e covers the floor; this proves the
    larger docker template is published and dockerd comes up there
    too."""
    template = template_name_for(
        language="docker", cpu_vcpus=4.0, memory_mb=4096,
    )
    assert template == "mcpolis-docker-cpu4-ram4096"
    instance = f"e2e-m3-{TEST_RUN_ID}"
    service = make_service(instance=instance, on_timeout_seconds=120)
    upstream = make_docker_upstream("m3")
    errlog = StringIO()
    try:
        async with service.session(
            session_id=instance,
            org_id=f"acme-m3-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_resources(4.0, 4096),
            denylist=(),
            errlog=errlog,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with session:
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=DOCKER_INITIALIZE_TIMEOUT,
                )
                assert init_result.serverInfo.name
                tools = await asyncio.wait_for(
                    session.list_tools(), timeout=TOOL_CALL_TIMEOUT,
                )
                assert len(tools.tools) > 0, (
                    f"mcp/everything on {template} should expose tools"
                )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                f"{template} not published on the active E2B account — "
                "run `cd runner/e2b-templates && make build` first.",
            )
        tail = errlog.getvalue()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise


# ---------------------------------------------------------------------------
# E2B-M11 — a DOCKER MCP must survive a wake, not just a cold start
# ---------------------------------------------------------------------------


# One idle window plus a docker create and a wake.
@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_e2b_m11_docker_mcp_survives_a_wake_broad_matrix() -> None:
    """Waking a docker-language sandbox, which nothing covered before.

    The wake fix reuses the sandbox and starts a FRESH ``docker run -i``
    inside it, skipping ``_start_docker_daemon`` on the reasoning that
    dockerd restores with the frozen microVM. The snapshot-resume path
    has always relied on that, but the wake made it load-bearing in a
    second place and review flagged it as never actually checked:
    every docker test here does create-then-initialize and none of them
    pauses.

    If dockerd does NOT survive the snapshot, the respawned
    ``docker run`` cannot reach the daemon socket and this fails at
    initialize. That is the whole point of the test.

    Persistence plus reuse is wired so cycle 2 lands on the SAME
    sandbox; without it the reopen would fresh-create and boot a fresh
    daemon, proving nothing.
    """
    instance = f"e2e-m11-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    service = make_service(
        instance=instance, persistence=persistence, reuse_on_restart=True,
    )
    upstream = make_docker_upstream("m11")
    org_id = f"acme-m11-{TEST_RUN_ID}"
    errlog = LogBuffer()

    async def cycle(index: int, *, sleep_first: bool) -> int:
        """Open a session, optionally idle into a pause, count tools."""
        session_id = f"{instance}-c{index}"
        async with service.session(
            session_id=session_id,
            org_id=org_id,
            upstream=upstream,
            resources=make_resources(2.0, 4096),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            tool_count = -1
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=DOCKER_INITIALIZE_TIMEOUT,
                )
                if sleep_first:
                    await asyncio.sleep(REATTACH_WAIT_SECONDS)
                    assert sandbox_session.transport_failed is not None
                    assert sandbox_session.transport_failed.is_set(), (
                        "the docker sandbox did not pause within the idle "
                        "window, so this cycle proves nothing about wakes"
                    )
                else:
                    listed = await asyncio.wait_for(
                        session.list_tools(), timeout=TOOL_CALL_TIMEOUT,
                    )
                    tool_count = len(listed.tools)
            # Must run INSIDE the session context: the mark is read by
            # the teardown that the ``async with`` exit triggers, so a
            # return above it would leave the sandbox to be killed and
            # cycle 3 would fresh-create instead of waking.
            service.mark_session_preserve_on_close(session_id)
        return tool_count

    try:
        first = await cycle(1, sleep_first=False)
        assert first > 0, "the docker MCP must expose tools on a cold start"

        # Idle past the pause window; the session ends by design.
        await cycle(2, sleep_first=True)

        # The wake: same sandbox, fresh ``docker run``, and the daemon
        # has to still be there.
        after_wake = await cycle(3, sleep_first=False)
        assert after_wake == first, (
            "after a wake the docker MCP must expose the same tools. A "
            "failure to initialize here means dockerd did NOT survive "
            f"the snapshot; got {after_wake} tools against {first}"
        )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-docker-cpu2-ram4096 not published — run the "
                "template grid build first.",
            )
        tail = errlog.get_output()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise
    finally:
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        if ref is not None and ref.sandbox_id is not None:
            with contextlib.suppress(Exception):
                await make_test_client().kill_sandbox(ref.sandbox_id)
