"""Router-level invocation edge cases (ROUTE-5 .. ROUTE-8, ROUTE-13).

Companion to ``test_tool_router.py``: pins the dispatch/heal/retry
contract on the awkward inputs — an upstream the router doesn't know,
upstream-side McpErrors that are NOT transport stalls (so must NOT heal),
discovery/dispatch divergence (a cached tool still dispatches when a
fresh list would fail), and a heal that tolerates an OAuth session-close
failure. Same builders/fakes as the sibling tool-router tests.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import McpError

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import DiscoveredTool, ToolAnnotations
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import upstream_connection_service as ucs_module
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from tests.unit._state_seed import seed_shared_session, seed_user_session
from tests.unit.factories import (
    make_discovered_tool,
    make_upstream_definition,
)


# --- ROUTE-6: route_call to a tool whose upstream is missing from the
#     router's own ``_upstreams`` (the registry resolved it, but the
#     router-side merge-defaults lookup is unguarded). ----------------


@pytest.mark.asyncio
async def test_route_call_upstream_missing_from_router_returns_error(
    tmp_path: Path,
) -> None:
    """ROUTE-6 [BUG?]: the registry and the router each hold their own
    ``_upstreams`` map. ``resolve_tool`` succeeds off the registry's map,
    then ``route_call`` does ``upstream = self._upstreams[upstream_id]`` on
    the router's map — UNGUARDED. If the two drift (an upstream registered
    on the registry but not the router), that bare subscript raises
    ``KeyError`` and crashes the request instead of returning a clean,
    opaque error result.

    INTENDED: a tool whose upstream the router can't resolve degrades to a
    ``CallToolResult(isError=True)`` (opaque), never a raw ``KeyError``."""
    upstream = make_upstream_definition(id="gh")
    cm = UpstreamClientManager([upstream])
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")],
            isError=False,
        ),
    )
    seed_shared_session(cm, "gh", session=session)

    # Registry KNOWS gh (so resolve_tool returns ("gh", "x")) ...
    registry = ToolRegistry([upstream], cm)
    registry._tools = [make_discovered_tool(upstream_id="gh", original_name="x")]

    # ... but the ROUTER's _upstreams is built from an EMPTY list, so the
    # router-side lookup diverges.
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cm, audit, [],  # <- router doesn't know gh
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="gh__x",
        arguments={},
        user_id="alice",
        session_id=None,
    )
    assert result.isError, "a router/registry upstream drift must not crash"


# --- ROUTE-7 / ROUTE-8: upstream-side McpErrors that are NOT transport
#     stalls. They must surface as an opaque error WITHOUT a heal (no
#     fresh reconnect) and be audited as an error. ---------------------


class _NonStallHealManager:
    """service_account manager slice the router touches: a fixed session
    plus a ``reconnect_shared_fresh`` heal counter. A non-stall McpError
    must leave ``fresh_calls == 0``."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.fresh_calls = 0

    async def ensure_shared_connected(self, upstream: Any) -> None:
        pass

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        return self._session

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.fresh_calls += 1


def _make_mcp_error_router(
    tmp_path: Path, error: McpError, *, upstream_id: str = "gh",
) -> tuple[ToolRouter, _NonStallHealManager, FileAuditRepository]:
    upstream = make_upstream_definition(id=upstream_id)  # service_account
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=error)
    cm = _NonStallHealManager(session)
    registry = ToolRegistry([upstream], cast(Any, cm))
    registry._tools = [
        # readonly → retry_safe, to prove that even a retry-eligible tool
        # is NOT retried/healed on a non-stall server error.
        make_discovered_tool(
            upstream_id=upstream_id, original_name="do_thing",
            annotations=ToolAnnotations(readOnlyHint=True),
        ),
    ]
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cast(Any, cm), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    return router, cm, audit


def _audit_rows(audit: FileAuditRepository) -> list[dict[str, Any]]:
    path = audit._log_path  # pyright: ignore[reportPrivateUsage]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_route_call_tool_gone_mcp_error_opaque_no_heal(
    tmp_path: Path,
) -> None:
    """ROUTE-7: ``call_tool`` raises ``McpError(-32601)`` — the upstream
    answered "this method is gone" on a session it still honors. That is
    NOT a transport stall: the result must be an opaque error, the session
    must NOT be healed (no fresh reconnect), and the audit row is ``error``."""
    router, cm, audit = _make_mcp_error_router(
        tmp_path,
        McpError(mcp_types.ErrorData(
            code=mcp_types.METHOD_NOT_FOUND, message="no such tool",
        )),
    )
    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="gh__do_thing",
        arguments={}, user_id="alice", session_id=None,
    )
    assert result.isError
    assert "no such tool" not in (result.content[0].text or "")  # type: ignore[union-attr]
    assert "Reference:" in (result.content[0].text or "")  # type: ignore[union-attr]
    assert cm.fresh_calls == 0, "a non-stall McpError must not heal the session"
    rows = _audit_rows(audit)
    assert rows[-1]["response_status"] == "error"


@pytest.mark.asyncio
async def test_route_call_invalid_args_mcp_error_opaque_no_heal(
    tmp_path: Path,
) -> None:
    """ROUTE-8: ``call_tool`` raises ``McpError(-32602)`` (invalid params).
    Same contract as ROUTE-7 — opaque error, no heal, audited ``error``."""
    router, cm, audit = _make_mcp_error_router(
        tmp_path,
        McpError(mcp_types.ErrorData(
            code=mcp_types.INVALID_PARAMS, message="bad arg shape",
        )),
    )
    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="gh__do_thing",
        arguments={"x": 1}, user_id="alice", session_id=None,
    )
    assert result.isError
    assert "bad arg shape" not in (result.content[0].text or "")  # type: ignore[union-attr]
    assert cm.fresh_calls == 0, "an invalid-args McpError must not heal"
    rows = _audit_rows(audit)
    assert rows[-1]["response_status"] == "error"


# --- ROUTE-5: discovery/dispatch divergence. The registry holds a cached
#     tool entry; a FRESH ``list_tools`` would fail, but ``call_tool``
#     succeeds. Dispatch must run off the cached entry — it never re-lists
#     to dispatch. -----------------------------------------------------


@pytest.mark.asyncio
async def test_route_call_dispatches_off_cached_tool_when_list_would_fail(
    tmp_path: Path,
) -> None:
    """ROUTE-5: the session's ``list_tools`` is broken (a fresh discovery
    would fail) but ``call_tool`` works. ``route_call`` resolves the tool
    from the cached registry slice and dispatches — it must NOT depend on a
    live re-list, so the call succeeds and ``list_tools`` is never touched
    on the dispatch path."""
    upstream = make_upstream_definition(id="gh")  # service_account
    cm = UpstreamClientManager([upstream])
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=RuntimeError("discovery down"))
    session.call_tool = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="dispatched")],
            isError=False,
        ),
    )
    seed_shared_session(cm, "gh", session=session)

    registry = ToolRegistry([upstream], cm)
    registry._tools = [
        make_discovered_tool(upstream_id="gh", original_name="cached_tool"),
    ]
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cm, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="gh__cached_tool",
        arguments={"a": 1}, user_id="alice", session_id=None,
    )
    assert not result.isError
    assert result.content[0].text == "dispatched"  # type: ignore[union-attr]
    session.call_tool.assert_awaited_once_with("cached_tool", {"a": 1})
    session.list_tools.assert_not_awaited()


# --- ROUTE-13: heal tolerates an OAuth session-close failure. A per-user
#     stall whose cached session's task.close() raises must still EVICT
#     the dead session and let the heal return (no propagation). ---------


@pytest.mark.asyncio
async def test_oauth_stall_heal_tolerates_close_failure_and_evicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUTE-13: a per_user_oauth dispatch stalls; the heal evicts the
    cached per-user session by closing its connection task. If
    ``task.close()`` itself raises, the session must STILL be evicted (the
    manager pops it before awaiting close) and the heal must return — the
    dead session can't be left cached for the next call to inherit.

    Router-observable assertion: after the (stalled, non-retry-safe) call,
    the per-user session is gone from the manager, the heal didn't blow up
    the request (an opaque error is returned), and a fresh reconnect would
    be attempted on the next call."""
    upstream = make_upstream_definition(
        id="gh",
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )
    cm = UpstreamClientManager([upstream])

    session = MagicMock()
    # A transport stall (closed stream) on the cached session.
    import anyio

    session.call_tool = AsyncMock(side_effect=anyio.ClosedResourceError())
    # The per-user connection task whose close() raises.
    failing_task = MagicMock(name="ConnectionTask")
    failing_task.close = AsyncMock(side_effect=RuntimeError("close exploded"))
    failing_task.server_info = None
    failing_task.self_description = None
    seed_user_session(cm, "gh", "alice", session=session, task=failing_task)

    # The reconnect after eviction must NOT find a live session (we evicted
    # it), so it falls through to reconnect_with_stored_tokens. Stub that to
    # report "couldn't reconnect" so the call surfaces an opaque error
    # rather than needing a real OAuth dance — the point under test is the
    # eviction surviving the close failure, not the reconnect succeeding.
    reconnect_calls: list[str] = []

    async def fake_reconnect(**kwargs: Any) -> Any:
        from mcpolis.domain.services.upstream_connection_service import (
            DisconnectReason,
        )

        reconnect_calls.append(kwargs["effective_user"])
        return DisconnectReason.no_tokens

    monkeypatch.setattr(
        ucs_module, "reconnect_with_stored_tokens", fake_reconnect,
    )

    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], cm)
    # No idempotent/readonly hint → not retry_safe → single attempt, but the
    # stall still heals.
    registry._tools = [
        DiscoveredTool(
            upstream_id="gh", original_name="do_thing",
            prefixed_name="gh__do_thing", description="x",
            input_schema={"type": "object", "properties": {}},
        ),
    ]
    router = ToolRouter(
        registry, cm, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=AsyncMock(spec=ConnectionStore),
        server_url="http://localhost:8000",
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="gh__do_thing",
        arguments={}, user_id="alice", session_id=None,
    )

    # The request didn't crash on the close failure — it returned an opaque
    # error result.
    assert result.isError
    # The dead session was evicted despite close() raising.
    assert not cm.has_user_session("gh", "alice"), (
        "the dead per-user session must be evicted even when close() raises"
    )
    failing_task.close.assert_awaited_once()
