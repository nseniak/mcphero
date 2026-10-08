from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
import pytest
import structlog

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import DiscoveredTool, ToolAnnotations
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.lifecycle import McpEndpoints
from mcpolis.entrypoints.mcp_transport_security import mcp_transport_security
from tests.unit._mcp_http_calls import call_tool_over_http
from tests.unit.stall_client_manager_fake import StallClientManagerFake
from tests.unit.factories import (
    Gate,
    YieldingAuditRepository,
    cancel_while_gated,
    make_discovered_tool,
    make_upstream_definition,
)


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


# The stall-manager fake is shared; see stall_client_manager_fake.
_FakeStallManager = StallClientManagerFake


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 1: a client that cancels the gateway request mid-dispatch
    must NOT be audited as a successful call (CancelledError is a
    BaseException that bypasses ``except Exception``, so the finally used to
    default to "success"), must NOT heal the session (cancellation isn't a
    transport stall), and the abandoned op must be cancelled.

    Cancelled the way the MCP SDK does it, through the request's anyio
    cancel scope, which raises again at every ``await`` until the scope
    exits; and with an audit store whose write waits like Mongo's. A plain
    ``task.cancel()`` raises once, and ``FileAuditRepository`` never waits,
    so together they can't see a row lost to the cancel."""
    from mcpolis.domain.services import tool_router as tr_module

    upstream = make_upstream_definition(id="mee6")  # service_account
    upstream_gate = Gate()  # never opened: the upstream never answers
    op_cancelled = asyncio.Event()

    async def held_call(*_a: Any, **_k: Any) -> Any:
        try:
            await upstream_gate.hold()
        except asyncio.CancelledError:
            op_cancelled.set()
            raise
        raise AssertionError("the upstream call was never cut off")

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=held_call)
    session.send_ping = AsyncMock(return_value=mcp_types.EmptyResult())
    cm = _FakeStallManager(session)
    registry = ToolRegistry([upstream], cast(Any, cm))
    registry._tools = [
        make_discovered_tool(upstream_id="mee6", original_name="do_thing"),
    ]
    audit = YieldingAuditRepository()
    router = ToolRouter(
        registry, cast(Any, cm), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    tracked: list[tuple[Any, ...]] = []

    class _Stub:
        def track_async(self, *a: Any, **_k: Any) -> None:
            tracked.append(a)

    monkeypatch.setattr(tr_module, "get_analytics", lambda: _Stub())

    await cancel_while_gated(upstream_gate, lambda: router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id=None,
    ))

    assert op_cancelled.is_set(), "the abandoned op must be cancelled"
    assert cm.fresh_calls == 0, "cancellation is not a stall — no heal"
    assert len(audit.rows) == 1, "the cancelled call must leave its row"
    assert audit.rows[0].response_status == "cancelled", (
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
        async def reconnect_shared_fresh(
            self, upstream: Any, *, stale: Any = None,
        ) -> Any:
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


# --- 4xx pass-through: the caller's own mistake reaches the caller ----------


@pytest.mark.asyncio
async def test_client_error_status_reaches_the_caller(tmp_path: Path) -> None:
    """A 4xx from the upstream must tell the caller their request was wrong.

    Masking every upstream failure behind "Upstream tool call failed"
    protects internal hostnames and stack detail from a tool caller who
    may not be the admin who configured the server. But a 4xx means the
    upstream understood the request and rejected its CONTENTS, which is
    the caller's own mistake and the one thing they can act on.

    Production case: an Elasticsearch MCP returned 400 for a malformed
    ES|QL query. The user retried the same broken query because nothing
    told them it was broken.

    Only the status line crosses the boundary, and it is OUR canonical
    phrase for that code, never the upstream's text — so the URL in the
    original message cannot escape.
    """
    err = McpError(mcp_types.ErrorData(
        code=mcp_types.INTERNAL_ERROR,
        message=(
            "HTTP status client error (400 Bad Request) for url "
            "(https://logs-collector.infra-elk.example.internal/_query)"
        ),
    ))
    router, _call_tool, _cm = make_stall_router(
        tmp_path, annotations=None, call_behaviours=[err],
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id="s1",
    )

    assert result.isError
    text = result.content[0].text  # type: ignore[union-attr]
    assert "400 Bad Request" in text, (
        f"the caller must learn their request was rejected; got {text!r}"
    )
    assert "infra-elk" not in text and "https://" not in text, (
        f"the upstream's URL must never cross the boundary; got {text!r}"
    )
    assert "Reference:" in text, "the correlation id stays, for admin lookup"


@pytest.mark.asyncio
async def test_server_error_status_stays_opaque(tmp_path: Path) -> None:
    """A 5xx is about the infrastructure, so it stays hidden.

    This is the line the pass-through must not cross: 5xx bodies and
    messages are where internal hostnames, stack detail and library
    versions live, and they are not the caller's fault or the caller's
    business.
    """
    err = McpError(mcp_types.ErrorData(
        code=mcp_types.INTERNAL_ERROR,
        message=(
            "HTTP status server error (500 Internal Server Error) for url "
            "(https://logs-collector.infra-elk.example.internal/_query)"
        ),
    ))
    router, _call_tool, _cm = make_stall_router(
        tmp_path, annotations=None, call_behaviours=[err],
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id="s1",
    )

    text = result.content[0].text  # type: ignore[union-attr]
    assert "500" not in text and "infra-elk" not in text, (
        f"a server-side failure must stay opaque; got {text!r}"
    )
    assert "Reference:" in text


@pytest.mark.asyncio
async def test_a_bare_number_is_not_mistaken_for_a_status(
    tmp_path: Path,
) -> None:
    """Detection keys on the canonical reason phrase, not a loose digit.

    A message mentioning "400" for any other reason (a row count, a
    port, a byte size) must not be reported as a client error. The
    check requires the digits AND the standard phrase together, so a
    coincidental number cannot trigger it.
    """
    err = McpError(mcp_types.ErrorData(
        code=mcp_types.INTERNAL_ERROR,
        message="index has 400 shards on host db-internal-7.example.internal",
    ))
    router, _call_tool, _cm = make_stall_router(
        tmp_path, annotations=None, call_behaviours=[err],
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id="s1",
    )

    text = result.content[0].text  # type: ignore[union-attr]
    assert "400" not in text and "db-internal" not in text, (
        f"a coincidental number must not leak anything; got {text!r}"
    )


# --- a caller's own bad request is not an application error ----------------


async def _level_of_failure(
    tmp_path: Path, message: str, code: int = mcp_types.INTERNAL_ERROR,
) -> str:
    """Route one call that fails with *message*; return its log level."""
    err = McpError(mcp_types.ErrorData(code=code, message=message))
    router, _call_tool, _cm = make_stall_router(
        tmp_path, annotations=None, call_behaviours=[err],
    )
    with structlog.testing.capture_logs() as logs:
        await router.route_call(
            org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
            arguments={}, user_id="alice", session_id="s1",
        )
    failures = [e for e in logs if e.get("event") == "tool.call.failed"]
    assert len(failures) == 1, f"expected one failure log, got {failures}"
    return str(failures[0]["log_level"])


@pytest.mark.asyncio
async def test_a_bad_request_is_logged_as_a_warning(tmp_path: Path) -> None:
    """A malformed request is the caller's mistake, not our outage.

    Sentry turns ERROR-level records into issues, so logging these as
    errors means every user typo raises an alert on the operator's
    dashboard and has to be resolved by hand — where it reopens the
    next time anyone mistypes. Sentry MCPOLIS-BACKEND-17 was exactly
    that: one malformed ES|QL query, filed as a platform fault.

    WARNING keeps the record, with its traceback, in the searchable
    logs where it is useful for analysis, and below the threshold that
    pages a human.
    """
    level = await _level_of_failure(
        tmp_path, "HTTP status client error (400 Bad Request) for url (x)",
    )
    assert level == "warning", (
        f"a caller's bad request must not page anyone; logged as {level}"
    )


@pytest.mark.asyncio
async def test_an_auth_failure_is_still_an_error(tmp_path: Path) -> None:
    """401 and 403 are 4xx but they are OUR problem, so they still page.

    An upstream answering 403 usually means its credentials expired or
    were revoked — the operator has to go and fix something. Quietly
    demoting it along with the typos would hide a real outage behind a
    rule written for typos. This upstream really did throw 403s in July
    for that reason.
    """
    level = await _level_of_failure(
        tmp_path, "HTTP status client error (403 Forbidden) for url (x)",
    )
    assert level == "error", (
        f"an auth failure must still raise an issue; logged as {level}"
    )


@pytest.mark.asyncio
async def test_a_server_failure_is_still_an_error(tmp_path: Path) -> None:
    """The control: 5xx is untouched by any of this."""
    level = await _level_of_failure(
        tmp_path,
        "HTTP status server error (500 Internal Server Error) for url (x)",
    )
    assert level == "error"


# --- an invalid-params answer is the caller's mistake too -------------------
#
# Sentry MCPOLIS-BACKEND-1B: an AI client called a tool without a required
# argument, and the upstream (a Rust rmcp server) answered with JSON-RPC
# -32602 rather than an HTTP 400. Same mistake, same verdict.

# rmcp's own wording for a missing argument (serde's message).
_SERDE_MISSING_FIELD = (
    "failed to deserialize parameters: missing field `query_body`"
)


async def _text_of_failure(tmp_path: Path, err: McpError) -> str:
    """Route one call that fails with *err*; return what the caller sees."""
    router, _call_tool, _cm = make_stall_router(
        tmp_path, annotations=None, call_behaviours=[err],
    )
    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id="s1",
    )
    assert result.isError
    return result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_invalid_params_is_logged_as_a_warning(tmp_path: Path) -> None:
    """A missing or wrong argument must not raise a Sentry issue.

    The caller chose the arguments, so this is their mistake, exactly
    like the HTTP 400 case above. Production put it on the operator's
    dashboard as an application error.
    """
    level = await _level_of_failure(
        tmp_path, _SERDE_MISSING_FIELD, code=mcp_types.INVALID_PARAMS,
    )
    assert level == "warning", (
        f"a caller's bad arguments must not page anyone; logged as {level}"
    )


@pytest.mark.asyncio
async def test_invalid_params_tells_the_caller_in_our_own_words(
    tmp_path: Path,
) -> None:
    """The caller learns their arguments were rejected, and nothing more.

    "Invalid params" is the JSON-RPC spec's name for the code, which an
    AI client can act on: re-read the tool's schema and call again. The
    upstream's message is never copied, because it can quote the
    caller's data or the server's internals.
    """
    text = await _text_of_failure(tmp_path, McpError(mcp_types.ErrorData(
        code=mcp_types.INVALID_PARAMS, message=_SERDE_MISSING_FIELD,
    )))
    assert "(Invalid params)" in text, (
        f"the caller must learn their arguments were rejected; got {text!r}"
    )
    assert "query_body" not in text and "deserialize" not in text, (
        f"the upstream's own message must never cross; got {text!r}"
    )
    assert "Reference:" in text


@pytest.mark.asyncio
async def test_invalid_params_with_an_auth_status_still_pages(
    tmp_path: Path,
) -> None:
    """An expired credential keeps its alert, whatever code wraps it.

    If an upstream reports its backend's 403 inside an invalid-params
    answer, the 403 is what matters: someone has to renew a credential.
    The status check runs first so it wins.
    """
    err = McpError(mcp_types.ErrorData(
        code=mcp_types.INVALID_PARAMS,
        message="HTTP status client error (403 Forbidden) for url (x)",
    ))
    level = await _level_of_failure(
        tmp_path, str(err.error.message), code=mcp_types.INVALID_PARAMS,
    )
    assert level == "error", (
        f"an auth failure must still raise an issue; logged as {level}"
    )
    text = await _text_of_failure(tmp_path, err)
    assert "(403 Forbidden)" in text, text


@pytest.mark.asyncio
async def test_method_not_found_is_still_an_error(tmp_path: Path) -> None:
    """-32601 is not the caller's mistake, so it keeps paging.

    The caller only picked a tool from our list. An upstream that does
    not implement the method behind it needs an operator, and nothing
    the caller types can fix it. Its text stays opaque.
    """
    level = await _level_of_failure(
        tmp_path, "tools/call", code=mcp_types.METHOD_NOT_FOUND,
    )
    assert level == "error"
    text = await _text_of_failure(tmp_path, McpError(mcp_types.ErrorData(
        code=mcp_types.METHOD_NOT_FOUND, message="tools/call",
    )))
    assert "(" not in text, f"no status may be surfaced; got {text!r}"


@pytest.mark.asyncio
async def test_invalid_request_is_still_an_error(tmp_path: Path) -> None:
    """-32600 means the request ENVELOPE was rejected.

    Our MCP client library builds the envelope, not the caller, so a
    rejected one is a protocol mismatch between us and the upstream.
    """
    level = await _level_of_failure(
        tmp_path, "Invalid request", code=mcp_types.INVALID_REQUEST,
    )
    assert level == "error"


@pytest.mark.asyncio
async def test_tool_call_without_a_live_session_writes_an_error_row(
    tmp_path: Path,
) -> None:
    """A call that cannot get an upstream session on its first attempt
    used to stop with no audit row. It must leave an ``error`` row, and
    the row must never carry the call's arguments."""
    upstream = make_upstream_definition(
        id="notion",
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )
    client_manager = UpstreamClientManager([upstream])
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(upstream_id="notion", original_name="search"),
    ]
    router = ToolRouter(
        registry, client_manager, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=None,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="notion__search",
        arguments={"api_key": "sk-live-do-not-store-me"},
        user_id="alice",
        session_id="sess1",
    )

    assert result.isError
    rows = await audit.search(DEFAULT_ORG_ID, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "notion__search"
    assert row["user_id"] == "alice"
    assert row["response_status"] == "error"
    assert row["error_message"]
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "sk-live-do-not-store-me" not in raw
    assert "api_key" not in raw


class RefuseSecondConnectFake(StallClientManagerFake):
    """Hands out the session once; every later connect fails, so the
    retry after a stall is refused ("not currently available")."""

    async def ensure_shared_connected(self, upstream: Any) -> Any:
        del upstream
        self.ensure_calls += 1
        if self.ensure_calls > 1:
            raise RuntimeError("sandbox gone")
        return self.session


class ResolveRaisesRouter(ToolRouter):
    """Session lookup itself blows up (a storage error, say)."""

    async def _resolve_session(
        self, org_id: str, upstream: Any, user_id: str,
    ) -> Any:
        del org_id, upstream, user_id
        raise RuntimeError("store unreachable")


class FailingAuditRepository(FileAuditRepository):
    """An audit store that is down."""

    async def log(self, org_id: str, entry: Any) -> None:
        del org_id, entry
        raise RuntimeError("audit store down")


def make_retry_refused_router(tmp_path: Path) -> tuple[ToolRouter, FileAuditRepository]:
    upstream = make_upstream_definition(id="mee6")
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=[asyncio.TimeoutError()])
    client_manager = RefuseSecondConnectFake(session)
    registry = ToolRegistry([upstream], cast(Any, client_manager))
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(
            upstream_id="mee6", original_name="do_thing",
            annotations=ToolAnnotations(idempotentHint=True),
        ),
    ]
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cast(Any, client_manager), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    return router, audit


@pytest.mark.asyncio
async def test_refused_retry_row_carries_the_message_the_caller_saw(
    tmp_path: Path,
) -> None:
    router, audit = make_retry_refused_router(tmp_path)

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="mee6__do_thing",
        arguments={}, user_id="alice", session_id=None,
    )

    assert result.isError
    first = result.content[0]
    assert isinstance(first, mcp_types.TextContent)
    assert "not currently available" in first.text
    rows = await audit.search(DEFAULT_ORG_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["response_status"] == "error"
    assert rows[0]["error_message"] == first.text


@pytest.mark.asyncio
async def test_tool_call_whose_session_lookup_raises_still_writes_a_row(
    tmp_path: Path,
) -> None:
    upstream = make_upstream_definition(id="github")
    client_manager = UpstreamClientManager([upstream])
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(upstream_id="github", original_name="create_issue"),
    ]
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ResolveRaisesRouter(
        registry, client_manager, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    with pytest.raises(RuntimeError):
        await router.route_call(
            org_id=DEFAULT_ORG_ID, prefixed_name="github__create_issue",
            arguments={}, user_id="alice", session_id=None,
        )

    rows = await audit.search(DEFAULT_ORG_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["response_status"] == "error"


@pytest.mark.asyncio
async def test_refused_call_still_answers_when_the_audit_store_is_down(
    tmp_path: Path,
) -> None:
    upstream = make_upstream_definition(
        id="notion", auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )
    client_manager = UpstreamClientManager([upstream])
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(upstream_id="notion", original_name="search"),
    ]
    router = ToolRouter(
        registry, client_manager,
        FailingAuditRepository(tmp_path / "audit.jsonl"), [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=None,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name="notion__search",
        arguments={}, user_id="alice", session_id=None,
    )

    assert result.isError


@pytest.mark.asyncio
async def test_successful_call_still_answers_when_the_audit_store_is_down(
    tmp_path: Path,
) -> None:
    router, _session, _ = make_tool_router(tmp_path)
    router._audit = FailingAuditRepository(tmp_path / "down.jsonl")  # pyright: ignore[reportPrivateUsage]

    with structlog.testing.capture_logs() as logs:
        result = await router.route_call(
            org_id=DEFAULT_ORG_ID, prefixed_name="github__create_issue",
            arguments={"title": "Bug"}, user_id="alice", session_id="s1",
        )

    assert not result.isError
    failures = [e for e in logs if e["event"] == "audit.write_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "error"


@pytest.mark.asyncio
async def test_denied_call_audit_survives_a_down_audit_store(
    tmp_path: Path,
) -> None:
    router, _session, _ = make_tool_router(tmp_path)
    router._audit = FailingAuditRepository(tmp_path / "down.jsonl")  # pyright: ignore[reportPrivateUsage]

    await router.audit_denied(
        DEFAULT_ORG_ID, user_id="alice", upstream_id="github",
        tool="github__create_issue", reason="MCP 'github' is disabled.",
    )


@pytest.mark.asyncio
async def test_closing_the_gateway_sessions_during_the_audit_write_keeps_the_row(
    tmp_path: Path,
) -> None:
    """At a deploy the shutdown leaves the gateway's session manager, which
    cancels, through anyio, the handler of every call still running. That
    is the cancel a gateway handler really gets at shutdown: the handler
    runs in the session manager's task group, so uvicorn's native
    ``Task.cancel()`` of the HTTP requests it cuts never reaches it. The
    row being written then is kept, and the cancel still ends the call,
    so the session manager is left."""
    router, _session, _ = make_tool_router(tmp_path)
    gate = Gate()
    audit = YieldingAuditRepository(gate=gate)
    router._audit = audit  # pyright: ignore[reportPrivateUsage]
    ended: list[str] = []
    server = FastMCP(
        name="gateway", streamable_http_path="/",
        transport_security=mcp_transport_security(),
    )

    @server.tool()
    async def create_issue() -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            await router.audit_denied(
                DEFAULT_ORG_ID, user_id="alice", upstream_id="github",
                tool="github__create_issue", reason="MCP 'github' is disabled.",
            )
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise
        ended.append("returned")
        return "denied"

    app = server.streamable_http_app()  # makes its session manager
    endpoints = McpEndpoints([server.session_manager])
    await endpoints.start()
    call = asyncio.create_task(call_tool_over_http(app, "create_issue"))
    await asyncio.wait_for(gate.reached.wait(), 10)
    closing = asyncio.create_task(endpoints.close())
    await asyncio.sleep(0.05)  # the cancel lands during the write
    gate.release.set()
    await asyncio.wait_for(closing, 10)
    await asyncio.gather(call, return_exceptions=True)

    assert [row.response_status for row in audit.rows] == ["denied"]
    assert ended == ["cancelled"]
