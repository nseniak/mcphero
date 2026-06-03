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
3. refresh again. With the fix this fails FAST (no 30s/90s hang) and
   marks the transport dead — it must NOT hang.
4. refresh once more. ``ensure_shared_connected`` now sees the dead
   transport and reconnects a fresh sandbox -> refresh succeeds.

Skips when ``E2B_API_KEY`` is unset, like the sibling e2e modules.
One sandbox, ~60s of E2B compute (~$0.02).

To run::

    cd runner/e2b-templates && make build      # one-time
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh \
        tests/integration/test_e2b_zombie_session_heal_e2e.py -v -s
"""
from __future__ import annotations

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
from tests.unit.factories import make_upstream_definition

E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK zombie-heal test skipped",
)

_SERVER_URL = "http://localhost:8000"
_INSTANCE = f"e2e-zombie-{TEST_RUN_ID}"


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
        infos = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": _INSTANCE},
        )
        assert infos, "expected a live sandbox to kill"
        for i in infos:
            sb = await client.connect_sandbox(i.sandbox_id)
            await sb.kill()

        # 3) Refresh after death must FAIL FAST — not hang 30s/90s — and
        #    mark the transport dead.
        started = time.monotonic()
        with pytest.raises(Exception):
            await _refresh(manager, registry, upstream, org_id)
        elapsed = time.monotonic() - started
        assert elapsed < 20.0, (
            f"refresh on a dead session must fail fast, took {elapsed:.1f}s "
            "(the pre-fix inert error-surfacing hung the full 30s)"
        )

        # 4) Next refresh heals: ensure_shared_connected sees the dead
        #    transport and reconnects a fresh sandbox.
        await _refresh(manager, registry, upstream, org_id)
        assert registry.get_all_tools(), (
            "refresh after a dead session must reconnect and return tools, "
            "not reuse the zombie (BrokenResourceError)"
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
