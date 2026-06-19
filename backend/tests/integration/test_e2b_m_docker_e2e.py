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
from io import StringIO

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.template_grid import template_name_for
from tests.integration._e2b_broad_matrix_helpers import (
    DOCKER_INITIALIZE_TIMEOUT,
    E2B_API_KEY,
    INITIALIZE_TIMEOUT,
    TEST_RUN_ID,
    TOOL_CALL_TIMEOUT,
    is_template_missing_error,
    make_docker_upstream,
    make_resources,
    make_service,
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
