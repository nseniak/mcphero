"""Real-SDK integration proof for the shared session-acquisition path
(``acquire_upstream_session``) and tool refresh, including the
DEFERRED_ATTACH reattach that caused the production "No active session
for upstream 'fetch3'" bug (a benign warm-but-detached service_account
sandbox was misclassified as failed and torn down).

Both the gateway tool-call path and the dashboard tool-refresh endpoint
now reattach through ``acquire_upstream_session``. This test drives that
helper against a live E2B sandbox:

1. Acquire a session on a freshly-connected service_account stdio
   upstream and ``tools/list`` (the tool-call contract).
2. Force the upstream into DEFERRED_ATTACH with cached metadata (what
   boot does, and what ``fetch3`` was in when it broke).
3. Acquire again — which must lazily reattach, NOT fail — then
   ``ToolRegistry.refresh_upstream`` and confirm tools come back. Before
   the fix this raised ``KeyError: No active session``.

Skips automatically when ``E2B_API_KEY`` is unset, mirroring the
sibling ``test_e2b_*_e2e.py`` modules. One scenario, ~60 s of E2B
compute (~$0.01).

To run::

    cd runner/e2b-templates && make build      # one-time
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh \
        tests/integration/test_e2b_tool_refresh_e2e.py -v -s
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from mcpolis.adapters.sandbox_e2b import (
    E2BSandboxService,
    RealE2BClient,
)
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
    reason="E2B_API_KEY not set — real-SDK tool-refresh test skipped",
)

_SERVER_URL = "http://localhost:8000"


def make_e2b_manager(
    upstream: object, org_id: str,
) -> UpstreamClientManager:
    """Wire a manager backed by the real E2B sandbox service — the same
    plumbing ``_build_sandbox_provider_plumbing`` builds in production,
    pinned to the ``e2b`` provider."""
    assert E2B_API_KEY is not None
    service = E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-refresh-{TEST_RUN_ID}",
        on_timeout_seconds=120,
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


@pytest.mark.asyncio
async def test_acquire_then_refresh_through_deferred_attach() -> None:
    org_id = f"acme-{TEST_RUN_ID}"
    upstream = make_upstream_definition(
        id=f"e2e-refresh-{TEST_RUN_ID}", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    manager = make_e2b_manager(upstream, org_id)
    registry = ToolRegistry([upstream], manager)

    try:
        # 1) Acquire a live session (service_account → ensure_shared_
        # connected → real sandbox) and list tools: the tool-call path.
        session = await acquire_upstream_session(
            org_id=org_id,
            upstream=upstream,
            effective_user="",
            connection_store=None,
            client_manager=manager,
            server_url=_SERVER_URL,
        )
        # acquire_upstream_session returns an already-initialized MCP
        # ClientSession (connect_shared did the handshake) — use it
        # directly.
        listed = await asyncio.wait_for(session.list_tools(), timeout=30.0)
        assert listed.tools, "server-everything should expose tools"

        # 2) Force DEFERRED_ATTACH with the cached metadata captured at
        # connect — the exact warm-but-detached state ``fetch3`` was in.
        state = manager.get_state(upstream.id)
        assert state is not None
        assert state.server_info is not None
        assert state.self_description is not None
        await manager.transition_to_deferred_attach(
            upstream.id,
            server_info=state.server_info,
            self_description=state.self_description,
        )

        # 3) Refresh: acquire must lazily REATTACH (not raise / not tear
        # the sandbox down), then refresh_upstream lists tools off the
        # reattached session. Pre-fix this raised "No active session".
        await acquire_upstream_session(
            org_id=org_id,
            upstream=upstream,
            effective_user="",
            connection_store=None,
            client_manager=manager,
            server_url=_SERVER_URL,
        )
        refreshed = await asyncio.wait_for(
            registry.refresh_upstream(upstream.id), timeout=30.0,
        )
        assert refreshed, (
            "refresh after DEFERRED_ATTACH must return tools off the "
            "reattached session, not raise 'No active session'"
        )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active "
                "account — run `cd runner/e2b-templates && make build`.",
            )
        raise
    finally:
        try:
            await manager.disconnect_upstream(upstream.id)
        except Exception:
            pass
