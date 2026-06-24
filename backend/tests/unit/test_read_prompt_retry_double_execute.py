"""Delivery-phase-aware stall recovery for ``resources/read`` /
``prompts/get`` (the read/prompt double-execute fix).

Background. ``route_call`` derives ``retry_safe`` from the tool's MCP
annotations; ``read_resource`` / ``get_prompt`` can't (no ``readOnlyHint``
exists for resources/prompts). Before the fix they were UNCONDITIONALLY
retry-safe, so a transport stall that happened AFTER the request reached
the upstream (the E2B #1128 silent post-reattach stall) would re-run a
possibly side-effecting read — a double-execute.

The fix keeps the valuable recovery but splits stalls by delivery phase
(``_DispatchVerb.retry_post_delivery`` + ``_is_post_delivery_stall``):

- PRE-delivery stall (a dead/closed transport — the request never left the
  gateway, e.g. the dead-idle-session case): still healed AND retried, for
  every verb. This is the recovery resources/read must keep.
- POST-delivery silent stall (``asyncio.TimeoutError`` from the liveness
  ping — request in flight, response lost): tools that DECLARED themselves
  safe (``readOnly``/``idempotent``) still retry; reads/prompts do NOT —
  they heal (so the next call is clean) but surface an error, running the
  upstream op exactly once.

Plus observability (fix C): when a read/prompt IS re-run (only ever on a
pre-delivery stall, the residual window the heuristic can't fully close),
``upstream.dispatch.read_reexecuted`` is emitted so a possible
double-effect is auditable.

Conventions (CLAUDE.md): top-level test functions only, no fixtures,
``make_*`` builders called explicitly, dependency injection (a subclassed
router overriding the ``_resolve_session`` + settle seams) over patching.
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import anyio
import mcp.types as mcp_types
import pytest
import structlog
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import ToolAnnotations
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import (  # pyright: ignore[reportPrivateUsage]
    ToolRouter,
    UpstreamRouterError,
    _DispatchVerb,
    _is_post_delivery_stall,
    _SessionResult,
)
from tests.unit.factories import (
    make_discovered_tool,
    make_upstream_definition,
)

UPSTREAM_ID = "flaky"
TOOL_NAME = "do_thing"
RESOURCE_URI = "file:///thing.txt"
USER = "alice@co.com"
REEXEC_EVENT = "upstream.dispatch.read_reexecuted"


def _timeout() -> BaseException:
    """A POST-delivery silent stall (what dispatch_with_liveness raises)."""
    return asyncio.TimeoutError()


def _closed() -> BaseException:
    """A PRE-delivery stall (a dead/closed transport)."""
    return anyio.ClosedResourceError()


class _StallThenSucceedSession:
    """Upstream session that stalls the FIRST call of each verb, then
    succeeds.

    Every verb increments its own counter the instant it is called — the
    model is "the request reached the upstream and its handler ran its side
    effect". The first call then raises ``stall_factory()`` (a transport
    stall); a second call (only happens if the router retries) returns a
    real result. The counter therefore measures how many times the
    upstream-side side effect actually executed."""

    def __init__(self, stall_factory: Any) -> None:
        self._stall = stall_factory
        self.read_calls = 0
        self.prompt_calls = 0
        self.tool_calls = 0

    async def read_resource(self, uri: AnyUrl) -> mcp_types.ReadResourceResult:
        self.read_calls += 1
        if self.read_calls == 1:
            raise self._stall()
        return mcp_types.ReadResourceResult(
            contents=[mcp_types.TextResourceContents(uri=uri, text="ok")],
        )

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None,
    ) -> mcp_types.GetPromptResult:
        self.prompt_calls += 1
        if self.prompt_calls == 1:
            raise self._stall()
        return mcp_types.GetPromptResult(messages=[])

    async def call_tool(
        self, name: str, arguments: dict[str, Any],
    ) -> mcp_types.CallToolResult:
        self.tool_calls += 1
        if self.tool_calls == 1:
            raise self._stall()
        return mcp_types.CallToolResult(content=[], isError=False)


class _HealOnlyClientManager:
    """Just enough of ``UpstreamClientManager`` for ``heal_stalled_session``
    on a ``service_account`` upstream: ``reconnect_shared_fresh`` succeeds
    so the heal completes and the retry loop proceeds."""

    def __init__(self) -> None:
        self.reconnects = 0

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.reconnects += 1


class _CountingAudit:
    """No-op audit sink (the router writes one row per dispatch)."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def log(self, org_id: str, entry: AuditEntry) -> None:
        self.entries.append(entry)


class _RecordingRouter(ToolRouter):
    """Router with session resolution injected (the fake session every
    time) and the OAuth dead-token settle probe recorded instead of run —
    so the loop's retry AND settle DECISIONS are exercised against the real
    ``_dispatch_with_recovery`` without any live upstream / OAuth stack."""

    def __init__(self, session: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fake_session = session
        self.settle_calls = 0

    async def _resolve_session(
        self, org_id: str, upstream: Any, user_id: str,
    ) -> _SessionResult:
        return _SessionResult(session=self._fake_session, effective_user="")

    async def _settle_after_unrecovered_stall(
        self, *, org_id: str, upstream: Any, effective_user: str,
    ) -> None:
        self.settle_calls += 1


def make_router(
    session: _StallThenSucceedSession,
    *,
    tool_annotations: ToolAnnotations | None = None,
) -> _RecordingRouter:
    """A router wired to *session*, a heal-only client manager, and a
    ``service_account`` upstream. *tool_annotations*, when given, seed one
    discovered tool so the ``call_tool`` path can derive ``retry_safe``."""
    upstream = make_upstream_definition(id=UPSTREAM_ID)
    client_manager = cast(UpstreamClientManager, _HealOnlyClientManager())
    registry = ToolRegistry([upstream], client_manager)
    if tool_annotations is not None:
        registry._tools = [
            make_discovered_tool(
                upstream_id=UPSTREAM_ID,
                original_name=TOOL_NAME,
                annotations=tool_annotations,
            ),
        ]
    return _RecordingRouter(
        session,
        registry,
        client_manager,
        cast(AuditRepository, _CountingAudit()),
        [upstream],
        PolicyEngine(SettingsConfig()),
        connection_store=None,
    )


# --- the classifier + the decision matrix (pure units) -----------------------


def test_is_post_delivery_stall_only_classifies_the_silent_timeout() -> None:
    """Only the synthesized ``asyncio.TimeoutError`` (request in flight,
    response lost) is post-delivery. Every already-dead-transport shape is
    pre-delivery (the request never left the gateway)."""
    assert _is_post_delivery_stall(asyncio.TimeoutError()) is True
    assert _is_post_delivery_stall(anyio.ClosedResourceError()) is False
    assert _is_post_delivery_stall(anyio.BrokenResourceError()) is False
    assert _is_post_delivery_stall(
        McpError(mcp_types.ErrorData(
            code=mcp_types.CONNECTION_CLOSED, message="closed",
        )),
    ) is False
    assert _is_post_delivery_stall(
        McpError(mcp_types.ErrorData(code=32600, message="Session terminated")),
    ) is False
    assert _is_post_delivery_stall(RuntimeError("not even a stall")) is False


def make_verb(
    *, retry_safe: bool, retry_post_delivery: bool,
) -> _DispatchVerb[None]:
    async def _unused_op(session: Any) -> None:
        raise AssertionError("op must not run in a may_retry unit test")

    return _DispatchVerb[None](
        audit_tool="x",
        op=_unused_op,
        retry_safe=retry_safe,
        retry_post_delivery=retry_post_delivery,
        is_tool_call=False,
        failure_log_event="x.failed",
        failure_log_fields={},
        on_session_error=lambda _e: None,
        on_dispatch_error=lambda _c: None,
        result_is_error=lambda _r: False,
    )


def test_may_retry_decision_matrix() -> None:
    timeout = asyncio.TimeoutError()
    closed = anyio.ClosedResourceError()

    # Annotation-declared safe tool: retries any stall.
    tool_safe = make_verb(retry_safe=True, retry_post_delivery=True)
    assert tool_safe.may_retry(timeout) is True
    assert tool_safe.may_retry(closed) is True

    # Non-idempotent tool: never retries.
    tool_unsafe = make_verb(retry_safe=False, retry_post_delivery=True)
    assert tool_unsafe.may_retry(timeout) is False
    assert tool_unsafe.may_retry(closed) is False

    # Read/prompt: retries a pre-delivery stall, never a post-delivery one.
    read_like = make_verb(retry_safe=True, retry_post_delivery=False)
    assert read_like.may_retry(closed) is True
    assert read_like.may_retry(timeout) is False


# --- read_resource / get_prompt behavioral (the fix) -------------------------


@pytest.mark.asyncio
async def test_read_resource_post_delivery_stall_runs_upstream_once() -> None:
    """The fix: a post-delivery silent stall does NOT re-run the read.
    Upstream invoked exactly once; the call surfaces an error; the session
    still heals; the OAuth dead-token probe runs (parity with a
    non-retry-safe tool)."""
    session = _StallThenSucceedSession(_timeout)
    router = make_router(session)

    with pytest.raises(UpstreamRouterError):
        await router.read_resource(
            org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
            original_uri=RESOURCE_URI, user_id=USER, session_id=None,
        )

    assert session.read_calls == 1, "post-delivery read must NOT double-execute"
    assert router.settle_calls == 1, "the unrecovered stall must settle OAuth"


@pytest.mark.asyncio
async def test_read_resource_pre_delivery_stall_recovers_via_retry() -> None:
    """Recovery preserved: a pre-delivery (dead-transport) stall retries on
    a fresh transport and returns the real result. Settle is NOT run (the
    retry reconnected)."""
    session = _StallThenSucceedSession(_closed)
    router = make_router(session)

    result = await router.read_resource(
        org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
        original_uri=RESOURCE_URI, user_id=USER, session_id=None,
    )

    assert session.read_calls == 2, "a pre-delivery stall must recover via retry"
    assert result.contents
    assert router.settle_calls == 0, "a recovered call must not settle"


@pytest.mark.asyncio
async def test_get_prompt_post_delivery_stall_runs_upstream_once() -> None:
    session = _StallThenSucceedSession(_timeout)
    router = make_router(session)

    with pytest.raises(UpstreamRouterError):
        await router.get_prompt(
            org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
            original_name="render", arguments={}, user_id=USER, session_id=None,
        )

    assert session.prompt_calls == 1, "post-delivery prompt must NOT double-execute"
    assert router.settle_calls == 1


@pytest.mark.asyncio
async def test_get_prompt_pre_delivery_stall_recovers_via_retry() -> None:
    session = _StallThenSucceedSession(_closed)
    router = make_router(session)

    await router.get_prompt(
        org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
        original_name="render", arguments={}, user_id=USER, session_id=None,
    )

    assert session.prompt_calls == 2, "a pre-delivery stall must recover via retry"
    assert router.settle_calls == 0


# --- call_tool unchanged: annotation-declared safety still governs ------------


@pytest.mark.asyncio
async def test_readonly_tool_still_retries_post_delivery_stall() -> None:
    """Tools are unchanged: a ``readOnly`` tool DECLARED itself repeatable,
    so it still retries even a post-delivery stall and recovers."""
    session = _StallThenSucceedSession(_timeout)
    router = make_router(
        session, tool_annotations=ToolAnnotations(readOnlyHint=True),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name=f"{UPSTREAM_ID}__{TOOL_NAME}",
        arguments={}, user_id=USER, session_id=None,
    )

    assert session.tool_calls == 2, "a readOnly tool retries any stall"
    assert not result.isError
    assert router.settle_calls == 0


@pytest.mark.asyncio
async def test_non_idempotent_tool_runs_once_and_settles() -> None:
    """Control: a non-idempotent tool is invoked once on any stall, surfaces
    an error, and settles OAuth — the protection reads/prompts now share for
    the post-delivery case."""
    session = _StallThenSucceedSession(_timeout)
    router = make_router(
        session,
        tool_annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False,
        ),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID, prefixed_name=f"{UPSTREAM_ID}__{TOOL_NAME}",
        arguments={}, user_id=USER, session_id=None,
    )

    assert session.tool_calls == 1, "a non-idempotent tool must not be retried"
    assert result.isError
    assert router.settle_calls == 1


# --- observability (fix C): a re-executed read is auditable ------------------


@pytest.mark.asyncio
async def test_pre_delivery_read_retry_emits_reexecuted_warning() -> None:
    """When a read IS re-run (only on a pre-delivery stall — the residual
    window the heuristic can't fully prove), emit
    ``upstream.dispatch.read_reexecuted`` so a possible double-effect is
    auditable."""
    session = _StallThenSucceedSession(_closed)
    router = make_router(session)

    with structlog.testing.capture_logs() as logs:
        await router.read_resource(
            org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
            original_uri=RESOURCE_URI, user_id=USER, session_id=None,
        )

    reexec = [e for e in logs if e.get("event") == REEXEC_EVENT]
    assert len(reexec) == 1, "a re-executed read must be logged"
    assert reexec[0]["stall"] == "ClosedResourceError"


@pytest.mark.asyncio
async def test_post_delivery_read_decline_emits_no_reexecuted_warning() -> None:
    """A declined post-delivery read never re-runs, so it must NOT log a
    re-execution."""
    session = _StallThenSucceedSession(_timeout)
    router = make_router(session)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(UpstreamRouterError):
            await router.read_resource(
                org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
                original_uri=RESOURCE_URI, user_id=USER, session_id=None,
            )

    assert not [e for e in logs if e.get("event") == REEXEC_EVENT]


@pytest.mark.asyncio
async def test_tool_retry_does_not_emit_read_reexecuted_warning() -> None:
    """The read-reexecuted marker is for reads/prompts only — a retried
    tool (which declared its own safety) must not emit it."""
    session = _StallThenSucceedSession(_timeout)
    router = make_router(
        session, tool_annotations=ToolAnnotations(readOnlyHint=True),
    )

    with structlog.testing.capture_logs() as logs:
        await router.route_call(
            org_id=DEFAULT_ORG_ID, prefixed_name=f"{UPSTREAM_ID}__{TOOL_NAME}",
            arguments={}, user_id=USER, session_id=None,
        )

    assert not [e for e in logs if e.get("event") == REEXEC_EVENT]
