from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
import structlog

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import DiscoveredTool, ToolAnnotations
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.factories import make_discovered_tool, make_upstream_definition


def make_tool_router(
    tmp_path: Path,
    upstream_id: str = "github",
    default_arguments: dict[str, dict[str, Any]] | None = None,
) -> tuple[ToolRouter, AsyncMock, FileAuditRepository]:
    """Build a ToolRouter with a mocked upstream session.

    Returns ``(router, mock_session, audit_service)``. Tests assert on
    ``mock_session.call_tool`` directly rather than fishing the session
    back out of ``client_manager._sessions`` (which pyright sees as a
    plain ClientSession).
    """
    upstream = make_upstream_definition(
        id=upstream_id,
        default_arguments=default_arguments or {},
    )
    client_manager = UpstreamClientManager([upstream])
    # Inject a mock session
    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")],
            isError=False,
        )
    )
    from tests.unit._state_seed import seed_shared_session
    seed_shared_session(client_manager, upstream_id, session=mock_session)

    audit_service = FileAuditRepository(tmp_path / "audit.jsonl")

    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [
        make_discovered_tool(upstream_id=upstream_id, original_name="create_issue"),
    ]

    router = ToolRouter(
        registry, client_manager, audit_service, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    return router, mock_session, audit_service


@pytest.mark.asyncio
async def test_route_call_proxies_to_correct_upstream(tmp_path: Path) -> None:
    router, mock_session, _ = make_tool_router(tmp_path)
    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={"title": "Bug"},
        user_id="alice",
        session_id="sess1",
    )
    assert not result.isError
    assert result.content[0].type == "text"
    # Verify the mock session was called with the right args
    mock_session.call_tool.assert_awaited_once_with("create_issue", {"title": "Bug"})


@pytest.mark.asyncio
async def test_route_call_emits_tool_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 (contrast to the resources/prompts gate): a tool call DOES emit
    the ``tool_called`` analytics event — the observability that
    read_resource / get_prompt must NOT emit."""
    from mcpolis.domain.services import tool_router as tr_module

    tracked: list[tuple[Any, ...]] = []

    class _Stub:
        def track_async(self, *a: Any, **k: Any) -> None:
            tracked.append((a, k))

    monkeypatch.setattr(tr_module, "get_analytics", lambda: _Stub())
    router, _, _ = make_tool_router(tmp_path)
    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={"title": "Bug"},
        user_id="alice",
        session_id="sess1",
    )
    assert len(tracked) == 1, "a tool call must emit exactly one analytics event"
    event_name = tracked[0][0][1]
    assert event_name == "tool_called"


@pytest.mark.asyncio
async def test_route_call_unknown_tool_returns_error(tmp_path: Path) -> None:
    router, _, _ = make_tool_router(tmp_path)
    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="unknown__tool",
        arguments={},
        user_id="alice",
        session_id=None,
    )
    assert result.isError
    assert "Unknown tool" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_route_call_logs_audit_entry(tmp_path: Path) -> None:
    router, _, audit_service = make_tool_router(tmp_path)
    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={"title": "Bug"},
        user_id="alice",
        session_id="sess1",
    )
    log_path = audit_service._log_path
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["user_id"] == "alice"
    assert entry["tool"] == "github__create_issue"
    assert entry["response_status"] == "success"
    assert entry["session_id"] == "sess1"
    assert entry["org_id"] == DEFAULT_ORG_ID


@pytest.mark.asyncio
async def test_route_call_threads_org_id_to_audit(tmp_path: Path) -> None:
    """The org_id passed to route_call must reach the audit log entry —
    not be silently dropped or replaced with a default."""
    router, _, audit_service = make_tool_router(tmp_path)
    await router.route_call(
        org_id="acme",
        prefixed_name="github__create_issue",
        arguments={},
        user_id="alice",
        session_id=None,
    )
    entry = json.loads(audit_service._log_path.read_text().strip())
    assert entry["org_id"] == "acme"


@pytest.mark.asyncio
async def test_route_call_records_latency(tmp_path: Path) -> None:
    router, _, audit_service = make_tool_router(tmp_path)
    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={},
        user_id="alice",
        session_id=None,
    )
    entry = json.loads(audit_service._log_path.read_text().strip())
    assert entry["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_route_call_merges_default_arguments(tmp_path: Path) -> None:
    router, mock_session, _ = make_tool_router(
        tmp_path,
        default_arguments={"create_issue": {"org": "acme"}},
    )
    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={"title": "Bug"},
        user_id="alice",
        session_id=None,
    )
    mock_session.call_tool.assert_awaited_once_with(
        "create_issue", {"title": "Bug", "org": "acme"}
    )


@pytest.mark.asyncio
async def test_route_call_upstream_error_returns_error_result(tmp_path: Path) -> None:
    router, mock_session, audit_service = make_tool_router(tmp_path)
    # Upstream exception content must NEVER surface to the MCP client
    # verbatim — internal URLs / hostnames / library versions would
    # leak. The router returns an opaque message with a correlation id
    # and logs the real exception server-side.
    secret_hostname = "internal-db.prod.example.com:5432"
    mock_session.call_tool = AsyncMock(
        side_effect=RuntimeError(f"connection to {secret_hostname} refused"),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={},
        user_id="alice",
        session_id=None,
    )
    assert result.isError
    text = result.content[0].text  # type: ignore[union-attr]
    assert secret_hostname not in text
    assert "Upstream tool call failed" in text
    assert "Reference:" in text

    entry = json.loads(audit_service._log_path.read_text().strip())
    assert entry["response_status"] == "error"


class _FakeStallManager:
    """The slice of ``UpstreamClientManager`` that ``acquire_upstream_session``
    + ``route_call``'s recovery touch for a service_account upstream. Returns a
    fixed scripted session and counts fresh reconnects so a stall-recovery
    test needs no real sandbox."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.ensure_calls = 0
        self.fresh_calls = 0

    async def ensure_shared_connected(self, upstream: Any) -> None:
        self.ensure_calls += 1

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        return self._session

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.fresh_calls += 1


def make_stall_router(
    tmp_path: Path,
    annotations: ToolAnnotations | None,
    call_behaviours: list[Any],
    upstream_id: str = "mee6",
) -> tuple[ToolRouter, AsyncMock, _FakeStallManager]:
    """A router over a service_account upstream whose session's ``call_tool``
    walks *call_behaviours* (an exception is raised; anything else returned).

    Returns (router, call_tool_mock, client_manager)."""
    upstream = make_upstream_definition(id=upstream_id)  # default: service_account
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=call_behaviours)
    client_manager = _FakeStallManager(session)
    registry = ToolRegistry([upstream], cast(Any, client_manager))
    registry._tools = [
        DiscoveredTool(
            upstream_id=upstream_id,
            original_name="do_thing",
            prefixed_name=f"{upstream_id}__do_thing",
            description="x",
            input_schema={"type": "object", "properties": {}},
            annotations=annotations,
        )
    ]
    audit_service = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cast(Any, client_manager), audit_service, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    return router, session.call_tool, client_manager


@pytest.mark.asyncio
async def test_route_call_retries_idempotent_tool_on_transport_stall(
    tmp_path: Path,
) -> None:
    # An idempotent tool whose first call hits a transport stall (the E2B
    # post-reattach stdout stall) must heal the session (fresh reconnect) AND
    # retry on the fresh transport — the caller gets the real result, not an
    # opaque error.
    ok = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")], isError=False,
    )
    router, call_tool, client_manager = make_stall_router(
        tmp_path,
        annotations=ToolAnnotations(idempotentHint=True),
        call_behaviours=[asyncio.TimeoutError(), ok],
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="mee6__do_thing",
        arguments={},
        user_id="alice",
        session_id="s1",
    )

    assert not result.isError
    assert client_manager.fresh_calls == 1, "stall must trigger a fresh reconnect"
    assert call_tool.await_count == 2, "idempotent tool must be retried after a stall"


@pytest.mark.asyncio
async def test_route_call_heals_but_does_not_retry_non_idempotent_tool(
    tmp_path: Path,
) -> None:
    # A tool with no idempotent/read-only hint must NOT be retried (it may have
    # side effects), but the stalled session must still be healed so the next
    # call recovers. The current call returns an opaque error.
    router, call_tool, client_manager = make_stall_router(
        tmp_path,
        annotations=None,
        call_behaviours=[asyncio.TimeoutError()],
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="mee6__do_thing",
        arguments={},
        user_id="alice",
        session_id="s1",
    )

    assert result.isError, "non-idempotent stall returns an error, not a silent retry"
    assert client_manager.fresh_calls == 1, "stall must still heal the session"
    assert call_tool.await_count == 1, "non-idempotent tool must not be retried"


# --- cancellation, heal-failure, and R8 markers (review reconciliation) --------


@pytest.mark.asyncio
async def test_route_call_cancelled_midflight_audited_cancelled_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 1: a client that cancels the gateway request mid-dispatch
    must NOT be audited as a successful call (CancelledError is a
    BaseException that bypasses ``except Exception``, so the finally used to
    default to "success"), must NOT heal the session (cancellation isn't a
    transport stall), and the abandoned op must be cancelled."""
    from mcpolis.domain.services import tool_router as tr_module

    upstream = make_upstream_definition(id="mee6")  # service_account
    op_started = asyncio.Event()
    op_cancelled = asyncio.Event()

    async def hang(*_a: Any, **_k: Any) -> Any:
        op_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            op_cancelled.set()
            raise

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=hang)
    session.send_ping = AsyncMock(return_value=mcp_types.EmptyResult())
    cm = _FakeStallManager(session)
    registry = ToolRegistry([upstream], cast(Any, cm))
    registry._tools = [
        make_discovered_tool(upstream_id="mee6", original_name="do_thing"),
    ]
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cast(Any, cm), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    tracked: list[tuple[Any, ...]] = []

    class _Stub:
        def track_async(self, *a: Any, **_k: Any) -> None:
            tracked.append(a)

    monkeypatch.setattr(tr_module, "get_analytics", lambda: _Stub())

    task = asyncio.create_task(router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id=None,
    ))
    await asyncio.wait_for(op_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert op_cancelled.is_set(), "the abandoned op must be cancelled"
    assert cm.fresh_calls == 0, "cancellation is not a stall — no heal"
    rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["response_status"] == "cancelled", (
        "a cancelled dispatch must not be audited as success"
    )
    assert tracked and tracked[0][2]["response_status"] == "cancelled"


@pytest.mark.asyncio
async def test_route_call_heal_failure_returns_opaque_error_not_raw(
    tmp_path: Path,
) -> None:
    """Review item 2: if the heal itself fails (E2B unreachable during the
    fresh reconnect), the router must RETURN an opaque error result — never
    let the heal's raw exception propagate (leaking detail) — and audit it as
    an error, not "success"."""
    upstream = make_upstream_definition(id="mee6")
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())

    class _HealFailsManager(_FakeStallManager):
        async def reconnect_shared_fresh(self, upstream: Any) -> None:
            raise RuntimeError("E2B unreachable at secret-host:5432")

    cm = _HealFailsManager(session)
    registry = ToolRegistry([upstream], cast(Any, cm))
    registry._tools = [
        make_discovered_tool(
            upstream_id="mee6", original_name="do_thing",
            annotations=ToolAnnotations(idempotentHint=True),  # retry_safe
        ),
    ]
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cast(Any, cm), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id=None,
    )

    assert result.isError, "a heal failure must surface as an error result"
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Reference:" in text
    assert "secret-host" not in text, "the heal's raw exception must not leak"
    rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[-1]["response_status"] == "error"


@pytest.mark.asyncio
async def test_route_call_emits_recovered_marker_on_stall_retry(
    tmp_path: Path,
) -> None:
    """R8 (review item 9): a stall+heal+retry that succeeds emits
    ``upstream.dispatch.recovered`` (so its inflated latency reads as a
    recovery, not a slow upstream) and tags the tool-completion log with
    attempts/stalled."""
    ok = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")], isError=False,
    )
    router, _call_tool, _cm = make_stall_router(
        tmp_path,
        annotations=ToolAnnotations(idempotentHint=True),
        call_behaviours=[asyncio.TimeoutError(), ok],
    )
    with structlog.testing.capture_logs() as logs:
        result = await router.route_call(
            org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
            arguments={}, user_id="alice", session_id="s1",
        )
    assert not result.isError
    recovered = [e for e in logs if e.get("event") == "upstream.dispatch.recovered"]
    assert len(recovered) == 1, "a recovered call must emit the R8 marker"
    assert recovered[0]["attempts"] == 2
    completed = [e for e in logs if e.get("event") == "upstream.tool_call.completed"]
    assert len(completed) == 1
    assert completed[0]["stalled"] is True
    assert completed[0]["attempts"] == 2


@pytest.mark.asyncio
async def test_route_call_no_recovered_marker_on_clean_call(
    tmp_path: Path,
) -> None:
    """A clean (non-stalled) call must NOT emit the recovery marker, and its
    completion log marks stalled=False / attempts=1."""
    router, _, _ = make_tool_router(tmp_path)
    with structlog.testing.capture_logs() as logs:
        await router.route_call(
            org_id=DEFAULT_ORG_ID, prefixed_name="github__create_issue",
            arguments={"title": "Bug"}, user_id="alice", session_id="s1",
        )
    assert not any(
        e.get("event") == "upstream.dispatch.recovered" for e in logs
    )
    completed = [e for e in logs if e.get("event") == "upstream.tool_call.completed"]
    assert len(completed) == 1
    assert completed[0]["stalled"] is False
    assert completed[0]["attempts"] == 1
