from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

import mcp.types as mcp_types
import structlog
from pydantic import AnyUrl

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry, is_transport_stall
from mcpolis.domain.services.upstream_connection_service import (
    SessionUnavailable,
    acquire_upstream_session,
    heal_stalled_session,
    settle_oauth_state_after_stall,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_T = TypeVar("_T")

# Dispatch stall-recovery tuning (R2/R5/R7).
#
# A dispatched MCP request (call_tool / read_resource / get_prompt) runs
# on a stdio ClientSession built with NO read timeout (see the R5 note in
# stdio_adapter), so a SILENT post-reattach stall (E2B #1128: a resumed
# sandbox whose stdout delivers a response or two and then goes quiet)
# would hang effectively unbounded. We bound it with ``asyncio.wait_for``
# at ``_DISPATCH_PROBE_INTERVAL`` — but a bare timeout cannot tell a
# silently-stalled transport from a LIVE server running a genuinely slow
# tool, and tearing a live-but-slow NON-idempotent tool down
# mid-execution (returning an opaque error after the side effect already
# happened) invites a double-execute on the user's manual retry (R2).
#
# So on each interval we send a ``ping`` on the same session: a prompt
# pong proves the transport is alive and we keep waiting (no teardown); a
# ping that ALSO times out / breaks proves the stream went silent and we
# raise a stall so the gateway heals + retries. The interval is therefore
# a liveness *cadence*, not a teardown bound — a slow tool is never torn
# down while its server still answers pings, which also means a legit
# 16-20s ``resources/read`` (R7) and a slow prompt render are safe.
_DISPATCH_PROBE_INTERVAL = 30.0
_DISPATCH_PING_TIMEOUT = 10.0


async def dispatch_with_liveness(
    session: Any,
    make_op: Callable[[], Awaitable[_T]],
    *,
    op_label: str,
    org_id: str,
    upstream_id: str,
    probe_interval: float = _DISPATCH_PROBE_INTERVAL,
    ping_timeout: float = _DISPATCH_PING_TIMEOUT,
) -> _T:
    """Run an MCP request, distinguishing a silent transport stall from a
    live-but-slow server via periodic pings. See the module constants.

    ``make_op()`` returns the awaitable for the actual request; it is
    invoked exactly once. Returns the op's result. Raises
    ``asyncio.TimeoutError`` (which ``is_transport_stall`` classifies →
    heal) when the transport has gone silent; otherwise propagates the
    op's own exception unchanged (a closed/broken stream surfaces
    promptly and is itself a stall; a real server error surfaces opaque).
    """
    op_task: asyncio.Task[_T] = asyncio.ensure_future(make_op())
    try:
        while True:
            done, _ = await asyncio.wait({op_task}, timeout=probe_interval)
            if op_task in done:
                # Completed (success or its own exception) — surface it.
                return op_task.result()
            # Still pending after the probe interval. Is the transport
            # alive (server merely slow) or silently stalled?
            #
            # ASSUMPTION (R2): a compliant MCP server ANSWERS ping — with a
            # pong, or at worst an error response — so any answer within
            # ping_timeout proves liveness. The MCP base protocol mandates
            # ping support, and the SDK server auto-answers it. A
            # NON-compliant upstream that SILENTLY DROPS an unknown ping
            # would look indistinguishable from a silent stall here → a
            # spurious heal of a live-but-slow tool (and, if it's a
            # non-idempotent tool, a possible double-execute on the
            # retry-safe retry). Narrow given the protocol guarantee, but
            # the failure mode lives here, not in the tool.
            try:
                await asyncio.wait_for(
                    session.send_ping(), timeout=ping_timeout,
                )
            except Exception as ping_exc:
                if is_transport_stall(ping_exc):
                    # No pong within ping_timeout (or the stream broke):
                    # the transport went silent. Signal a stall; the
                    # ``finally`` cancels the abandoned op.
                    logger.warning(
                        "upstream.dispatch.stall_detected",
                        org_id=org_id,
                        upstream_id=upstream_id,
                        op=op_label,
                        ping_error=type(ping_exc).__name__,
                    )
                    raise asyncio.TimeoutError(
                        f"{op_label}: transport went silent — ping "
                        f"unanswered within {ping_timeout}s"
                    ) from ping_exc
                # The server answered ping with an error — it is alive,
                # just slow on this op. Keep waiting.
                logger.info(
                    "upstream.dispatch.slow_but_alive",
                    org_id=org_id,
                    upstream_id=upstream_id,
                    op=op_label,
                    ping_error=type(ping_exc).__name__,
                )
            else:
                # Pong received: transport alive, server just slow.
                logger.info(
                    "upstream.dispatch.slow_but_alive",
                    org_id=org_id,
                    upstream_id=upstream_id,
                    op=op_label,
                )
    finally:
        if not op_task.done():
            op_task.cancel()
            try:
                await op_task
            except BaseException:
                # Swallow the cancellation / late failure of the
                # abandoned op: the surfaced outcome is the stall (or an
                # outer cancellation propagating through this finally).
                pass


@dataclass
class _DispatchVerb(Generic[_T]):
    """Per-verb knobs for ``ToolRouter._dispatch_with_recovery`` (R3/R4).

    The shared loop resolves the session, dispatches with ping-gated
    stall recovery, heals + retries on a transport stall, and audits —
    identically for call_tool / read_resource / get_prompt, so "forgot to
    heal" is structurally impossible. Everything that DIFFERS between the
    three verbs is captured here so the loop stays verb-agnostic.
    """

    audit_tool: str
    """Audit ``tool`` field — kept per-verb (R3): ``prefixed_name`` /
    ``resource:<id>:<uri>`` / ``prompt:<id>:<name>``. Also the op label
    in the dispatch/stall logs."""

    op: Callable[[Any], Awaitable[_T]]
    """The actual dispatch, given the resolved session."""

    retry_safe: bool
    """Whether a transport stall may transparently re-run the op. tools:
    only ``readOnly``/``idempotent``. resources/prompts: True — no
    ``readOnlyHint`` exists for them, so this is a documented assumption
    that increases shared-sandbox churn under synchronized heal
    (risk-b); accepted as reads being safe to repeat."""

    is_tool_call: bool
    """Tool-only-observability gate (R3): the
    ``upstream.tool_call.completed`` log + ``tool_called`` analytics fire
    ONLY for call_tool — else every resources/read & prompts/get would
    pollute the tool analytics + slow-tool dashboards. The audit row is
    written for ALL verbs (with the per-verb ``audit_tool``)."""

    failure_log_event: str
    failure_log_fields: dict[str, Any]
    """Structured-log event name + extra fields for an opaque dispatch
    failure (``tool.call.failed`` / ``resource.read.failed`` /
    ``prompt.get.failed``)."""

    on_session_error: Callable[[mcp_types.CallToolResult], _T]
    """Map a session-unavailable result to the verb's surface (R4):
    tools RETURN the actionable ``CallToolResult``; resources/prompts
    RAISE ``UpstreamRouterError`` carrying the same actionable text (so
    the gateway surfaces it via a clean Read/GetPrompt result)."""

    on_dispatch_error: Callable[[str], _T]
    """Map an opaque dispatch failure (given a correlation id) to the
    verb's surface (R4): tools RETURN ``CallToolResult(isError=True)``;
    resources/prompts RAISE ``UpstreamRouterError``."""

    result_is_error: Callable[[_T], bool]
    """Whether a *successful* dispatch nonetheless reports an error
    outcome — call_tool: ``result.isError``; resources/prompts: always
    False (those result types carry no error bit)."""


class ToolRouter:
    def __init__(
        self,
        tool_registry: ToolRegistry,
        client_manager: UpstreamClientManager,
        audit_service: AuditRepository,
        upstreams: list[UpstreamDefinition],
        policy_engine: PolicyEngine,
        connection_store: ConnectionStore | None = None,
        server_url: str = "http://localhost:8000",
    ) -> None:
        self._registry = tool_registry
        self._client_manager = client_manager
        self._audit = audit_service
        self._upstreams = {u.id: u for u in upstreams}
        self._policy_engine = policy_engine
        self._connection_store = connection_store
        self._server_url = server_url

    def register_upstream(self, upstream: UpstreamDefinition) -> None:
        """Keep ``_upstreams`` in sync with runtime add-upstream flows.

        ``route_call`` looks ``upstream_id`` up here to get the merged
        default arguments. Without this, calls to upstreams added via
        the dashboard API after startup raise ``KeyError`` and bubble
        up as a generic error result instead of running the tool.
        """
        self._upstreams[upstream.id] = upstream

    def unregister_upstream(self, upstream_id: str) -> None:
        self._upstreams.pop(upstream_id, None)

    async def _resolve_admin_oauth_owner(
        self, org_id: str, upstream_id: str,
    ) -> str | None:
        """Return the admin who currently owns the admin_oauth slot.

        ``admin_oauth`` is single-slot by design: at most one admin's
        token is stored for an upstream at a time. The connect handler
        clears every other admin's token for the upstream before
        writing the caller's, so this lookup typically finds at most
        one match. If the data ever has more than one (e.g. legacy
        rows from before the single-slot invariant was enforced), pick
        the most recently refreshed; tie-break lexicographically for
        deterministic test runs. Returns ``None`` when no admin holds
        the slot.
        """
        if self._connection_store is None:
            return None
        admin_emails = self._policy_engine.get_admin_emails()
        candidates: list[tuple[datetime, str, str]] = []
        for email in admin_emails:
            token = await self._connection_store.get_user_token(
                org_id, email, upstream_id,
            )
            if token is None:
                continue
            ts = (
                token.refresh_token_created_at
                or token.updated_at
                or datetime.fromtimestamp(0, tz=UTC)
            )
            candidates.append((ts, email, email))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0].timestamp(), x[1]))
        return candidates[0][2]

    def _admin_unavailable_error(
        self, upstream: UpstreamDefinition
    ) -> mcp_types.CallToolResult:
        """Tool unavailable due to admin-side issue (not running, or admin not signed in)."""
        admins = self._policy_engine.get_admin_emails()
        admin_list = ", ".join(admins) if admins else "an administrator"
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(
                type="text",
                text=(
                    f"The '{upstream.display_name}' tool is not currently "
                    f"available. Please tell the user to contact an "
                    f"administrator: {admin_list}."
                ),
            )],
            isError=True,
        )

    async def route_call(
        self,
        org_id: str,
        prefixed_name: str,
        arguments: dict[str, Any],
        user_id: str,
        session_id: str | None,
    ) -> mcp_types.CallToolResult:
        resolved = self._registry.resolve_tool(prefixed_name)
        if resolved is None:
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text=f"Unknown tool: {prefixed_name}",
                )],
                isError=True,
            )

        upstream_id, original_name = resolved
        upstream = self._upstreams.get(upstream_id)
        if upstream is None:
            # The registry resolved the tool off its OWN _upstreams map,
            # but the router's map lacks the upstream (registry/router
            # drift). Guard the subscript and return a clean opaque error,
            # mirroring read_resource / get_prompt — never a raw KeyError
            # (ROUTE-6).
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text=f"Unknown tool: {prefixed_name}",
                )],
                isError=True,
            )

        # Merge default arguments
        defaults = upstream.default_arguments.get(original_name, {})
        merged_arguments = {**arguments, **defaults}

        # A tool call may be retried after a transport stall only if the tool
        # declares itself safe to repeat (idempotent or read-only); a blind
        # retry of a side-effecting tool could double-execute it. Either way a
        # stall on a service_account upstream heals the shared session (drop +
        # fresh reconnect) so the next call lands on a clean transport.
        flags = self._registry.get_tool_annotations(upstream_id, original_name)
        retry_safe = bool(flags.get("readOnly") or flags.get("idempotent"))

        def _on_session_error(
            err: mcp_types.CallToolResult,
        ) -> mcp_types.CallToolResult:
            # Tools RETURN the actionable session-unavailable result.
            return err

        def _on_dispatch_error(
            correlation_id: str,
        ) -> mcp_types.CallToolResult:
            # Don't leak internal exception content (URLs, hostnames, stack
            # detail, library versions) to MCP clients — the full exception is
            # logged server-side; return an opaque error with a correlation id
            # the user can quote when asking an admin to investigate.
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text=(
                        f"Upstream tool call failed. "
                        f"Reference: {correlation_id}"
                    ),
                )],
                isError=True,
            )

        verb: _DispatchVerb[mcp_types.CallToolResult] = _DispatchVerb(
            audit_tool=prefixed_name,
            op=lambda s: s.call_tool(original_name, merged_arguments),
            retry_safe=retry_safe,
            is_tool_call=True,
            failure_log_event="tool.call.failed",
            failure_log_fields={"tool": prefixed_name},
            on_session_error=_on_session_error,
            on_dispatch_error=_on_dispatch_error,
            result_is_error=lambda r: r.isError,
        )
        return await self._dispatch_with_recovery(
            org_id=org_id,
            upstream=upstream,
            user_id=user_id,
            session_id=session_id,
            verb=verb,
        )

    async def _dispatch_with_recovery(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        user_id: str,
        session_id: str | None,
        verb: _DispatchVerb[_T],
    ) -> _T:
        """Resolve → dispatch → heal → retry, shared by all three verbs.

        Hoisted out of the original ``route_call`` so ``resources/read`` and
        ``prompts/get`` inherit the SAME stall recovery the tool path has —
        the structural fix for "forgot to heal" (the read_resource /
        get_prompt gap). Auth resolution, the audit row, and (tool-only,
        per R3) the observability log + analytics live here; the verb
        supplies only its op + per-verb surfaces (see ``_DispatchVerb``).

        Deliberately NOT merged with ``acquire_and_refresh_with_recovery``:
        refresh never receives a session, and dispatch needs the per-auth
        ``effective_user`` for eviction plus the per-verb error/audit
        mapping.
        """
        auth_identity = f"{upstream.auth.mode.value}:{upstream.id}"
        max_attempts = 2 if verb.retry_safe else 1

        start = time.monotonic()
        response_status = "success"
        did_call = False
        attempts = 0
        stalled = False
        try:
            for attempt in range(max_attempts):
                session_result = await self._resolve_session(
                    org_id, upstream, user_id,
                )
                if session_result.error is not None:
                    # Session unavailable. On the FIRST attempt no call ever
                    # ran, so surface without an audit/analytics row (R4 —
                    # preserves pre-recovery behaviour: route_call returned,
                    # resources/prompts raised before their finally). On a
                    # retry (did_call already True) it's a real failure.
                    if did_call:
                        response_status = "error"
                    return verb.on_session_error(session_result.error)
                assert session_result.session is not None
                session = session_result.session
                if session_result.auth_identity:
                    auth_identity = session_result.auth_identity

                did_call = True
                attempts = attempt + 1
                try:
                    result = await dispatch_with_liveness(
                        session,
                        lambda s=session: verb.op(s),
                        op_label=verb.audit_tool,
                        org_id=org_id,
                        upstream_id=upstream.id,
                    )
                    if verb.result_is_error(result):
                        response_status = "error"
                    return result
                except Exception as exc:
                    # NB: ``except Exception`` deliberately does NOT catch
                    # ``asyncio.CancelledError`` (a BaseException) — a
                    # cancelled dispatch (the client gave up) is not a
                    # transport stall, so it must propagate WITHOUT a heal,
                    # and the ``finally`` reclassifies its audit status off
                    # the in-flight exception (so it's never logged as
                    # "success").
                    is_last = attempt + 1 >= max_attempts
                    # A transport stall poisons the cached session (the E2B
                    # post-reattach stdout stall for the shared
                    # service_account session; an idle-closed HTTP connection
                    # for a per-user OAuth session). Drop it so this retry —
                    # and every later call — runs on a fresh transport
                    # instead of inheriting the dead one until the idle
                    # sweep. Healing happens even when the verb isn't
                    # retry-safe (max_attempts == 1): the current call still
                    # errors, but the next one recovers.
                    if is_transport_stall(exc):
                        stalled = True
                        try:
                            await heal_stalled_session(
                                org_id=org_id,
                                upstream=upstream,
                                effective_user=session_result.effective_user,
                                client_manager=self._client_manager,
                            )
                        except Exception:
                            # The heal itself failed (e.g. E2B unreachable
                            # during the fresh reconnect). Don't let it
                            # propagate raw — that would leak internal detail
                            # AND skip the opaque-error return below. Fall
                            # through to the opaque error; the next call
                            # retries the heal.
                            logger.exception(
                                "upstream.dispatch.heal_failed",
                                org_id=org_id,
                                upstream_id=upstream.id,
                                op=verb.audit_tool,
                            )
                        else:
                            if not is_last:
                                logger.warning(
                                    "upstream.dispatch.transport_stall_retry",
                                    org_id=org_id,
                                    upstream_id=upstream.id,
                                    op=verb.audit_tool,
                                    attempt=attempt,
                                )
                                continue
                            if not verb.retry_safe:
                                # Last (only) attempt of a NON-retry-safe
                                # verb: the heal evicted but nothing will
                                # reconnect, so a stall caused by REVOKED
                                # OAuth tokens would leave the token row —
                                # and the dashboard "Ready" — intact. Run
                                # the reconnect probe a retry-safe verb gets
                                # for free (its retry's _resolve_session
                                # reconnects + classifies), so §5.1 deletes
                                # dead tokens and the dashboard reflects the
                                # disconnect. No-op for service_account.
                                await settle_oauth_state_after_stall(
                                    org_id=org_id,
                                    upstream=upstream,
                                    effective_user=session_result.effective_user,
                                    connection_store=self._connection_store,
                                    client_manager=self._client_manager,
                                    server_url=self._server_url,
                                )
                    response_status = "error"
                    correlation_id = uuid.uuid4().hex[:12]
                    logger.exception(
                        verb.failure_log_event,
                        org_id=org_id,
                        upstream_id=upstream.id,
                        correlation_id=correlation_id,
                        **verb.failure_log_fields,
                    )
                    return verb.on_dispatch_error(correlation_id)
            # The loop always returns (success, opaque error, or session
            # error) or continues; it never falls through.
            raise AssertionError(
                "_dispatch_with_recovery loop exited without returning"
            )
        finally:
            if did_call:
                # If we're unwinding because an exception is propagating —
                # the caller cancelled the request, or a heal re-raised, or
                # an opaque-error adapter raised (resources/prompts) — the
                # dispatch did NOT complete successfully. Never audit it as
                # "success": reclassify off the in-flight exception type.
                # ``CancelledError`` → "cancelled" (client gave up, no
                # stall, no heal); anything else → "error".
                if response_status == "success":
                    pending_exc = sys.exc_info()[0]
                    if pending_exc is not None:
                        response_status = (
                            "cancelled"
                            if issubclass(pending_exc, asyncio.CancelledError)
                            else "error"
                        )
                latency_ms = (time.monotonic() - start) * 1000
                if stalled:
                    # R8: the retry path's ``latency_ms`` includes the full
                    # probe interval + ping + heal + re-dispatch, so a
                    # "successful" recovered call logs ~30s+. This marker
                    # (for ANY verb) tells operators the latency reflects a
                    # stall recovery, not a genuinely slow upstream.
                    logger.info(
                        "upstream.dispatch.recovered",
                        org_id=org_id,
                        upstream_id=upstream.id,
                        op=verb.audit_tool,
                        attempts=attempts,
                        response_status=response_status,
                        latency_ms=int(latency_ms),
                    )
                if verb.is_tool_call:
                    # Tool-only observability (R3): latency + outcome per
                    # dispatch on the structured-log surface so operators can
                    # grep "is upstream X slow" without Sentry traces or the
                    # audit table. Gated to call_tool — else every
                    # resources/read & prompts/get would pollute the tool
                    # analytics + slow-tool dashboards.
                    logger.info(
                        "upstream.tool_call.completed",
                        org_id=org_id,
                        upstream_id=upstream.id,
                        tool=verb.audit_tool,
                        response_status=response_status,
                        latency_ms=int(latency_ms),
                        attempts=attempts,
                        stalled=stalled,
                        auth_mode=upstream.auth.mode.value,
                        user_id=user_id,
                        session_id=session_id,
                    )
                entry = AuditEntry(
                    timestamp=datetime.now(UTC).isoformat(),
                    org_id=org_id,
                    user_id=user_id,
                    upstream_id=upstream.id,
                    auth_mode=upstream.auth.mode.value,
                    auth_identity=auth_identity,
                    tool=verb.audit_tool,
                    policy_decision="allowed",
                    response_status=response_status,
                    latency_ms=latency_ms,
                    session_id=session_id,
                )
                await self._audit.log(org_id, entry)
                if verb.is_tool_call:
                    get_analytics().track_async(
                        user_id,
                        "tool_called",
                        {
                            "upstream_id": upstream.id,
                            "tool_name": verb.audit_tool,
                            "auth_mode": upstream.auth.mode.value,
                            "response_status": response_status,
                            "latency_ms": int(latency_ms),
                            "had_session_id": session_id is not None,
                        },
                    )

    async def audit_denied(
        self,
        org_id: str,
        *,
        user_id: str,
        upstream_id: str,
        tool: str,
        reason: str,
        policy_rule: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record a policy-denied tool call.

        Enforcement happens upstream in the gateway controller (which
        short-circuits before ``route_call`` ever runs); this writes the
        matching ``denied`` audit row so the deny is as auditable as an
        allowed call. ``reason`` is a human-readable explanation (which
        MCP / which forbidden argument) surfaced in the Audit UI.
        """
        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            org_id=org_id,
            action="tool_call",
            user_id=user_id,
            upstream_id=upstream_id,
            tool=tool,
            policy_decision="denied",
            policy_rule=policy_rule or reason,
            response_status="denied",
            outcome="denied",
            error_message=reason,
            session_id=session_id,
        )
        await self._audit.log(org_id, entry)

    async def read_resource(
        self,
        org_id: str,
        upstream_id: str,
        original_uri: str,
        user_id: str,
        session_id: str | None,
    ) -> mcp_types.ReadResourceResult:
        """Forward a ``resources/read`` to the right upstream session.

        Routing mirrors ``route_call`` through the shared
        ``_dispatch_with_recovery`` loop: same ``_resolve_session`` chain
        (so ``per_user_oauth`` upstreams' resources are caller-scoped),
        same ping-gated stall recovery, same audit shape (``tool`` field
        carries the wrapped resource URI for traceability). On a
        session-not-available or dispatch-failure outcome the verb's
        adapters raise ``UpstreamRouterError`` with the user-facing
        message — the gateway controller maps that to a clean text
        ``ReadResourceResult`` (these result types carry no
        ``isError`` bit). ``retry_safe=True`` is a documented assumption
        (risk-b): no ``readOnlyHint`` exists for resources, so a read is
        taken to be safe to repeat.
        """
        upstream = self._upstreams.get(upstream_id)
        if upstream is None:
            raise UpstreamRouterError(f"Unknown upstream '{upstream_id}'.")

        def _on_session_error(
            err: mcp_types.CallToolResult,
        ) -> mcp_types.ReadResourceResult:
            # Preserve the ACTIONABLE message (contact admin / sign in on
            # /my-tools), re-raised so the gateway surfaces it — never
            # flattened into the opaque dispatch-failure wrapper (R4).
            raise UpstreamRouterError(_session_error_text(err))

        def _on_dispatch_error(
            correlation_id: str,
        ) -> mcp_types.ReadResourceResult:
            raise UpstreamRouterError(
                f"Upstream resource read failed. Reference: {correlation_id}",
            )

        verb: _DispatchVerb[mcp_types.ReadResourceResult] = _DispatchVerb(
            audit_tool=f"resource:{upstream_id}:{original_uri}",
            op=lambda s: s.read_resource(AnyUrl(original_uri)),
            retry_safe=True,
            is_tool_call=False,
            failure_log_event="resource.read.failed",
            failure_log_fields={"resource_uri": original_uri},
            on_session_error=_on_session_error,
            on_dispatch_error=_on_dispatch_error,
            result_is_error=lambda _r: False,
        )
        return await self._dispatch_with_recovery(
            org_id=org_id,
            upstream=upstream,
            user_id=user_id,
            session_id=session_id,
            verb=verb,
        )

    async def get_prompt(
        self,
        org_id: str,
        upstream_id: str,
        original_name: str,
        arguments: dict[str, str] | None,
        user_id: str,
        session_id: str | None,
    ) -> mcp_types.GetPromptResult:
        """Forward a ``prompts/get`` to the right upstream session.

        Counterpart to ``read_resource``; same auth-mode handling, same
        ping-gated stall recovery, same audit semantics, same
        ``retry_safe=True`` assumption (risk-b). ``arguments`` is
        forwarded unchanged to the upstream, mirroring ``call_tool``'s
        arguments passthrough.
        """
        upstream = self._upstreams.get(upstream_id)
        if upstream is None:
            raise UpstreamRouterError(f"Unknown upstream '{upstream_id}'.")

        def _on_session_error(
            err: mcp_types.CallToolResult,
        ) -> mcp_types.GetPromptResult:
            raise UpstreamRouterError(_session_error_text(err))

        def _on_dispatch_error(
            correlation_id: str,
        ) -> mcp_types.GetPromptResult:
            raise UpstreamRouterError(
                f"Upstream prompt failed. Reference: {correlation_id}",
            )

        verb: _DispatchVerb[mcp_types.GetPromptResult] = _DispatchVerb(
            audit_tool=f"prompt:{upstream_id}:{original_name}",
            op=lambda s: s.get_prompt(original_name, arguments),
            retry_safe=True,
            is_tool_call=False,
            failure_log_event="prompt.get.failed",
            failure_log_fields={"prompt": original_name},
            on_session_error=_on_session_error,
            on_dispatch_error=_on_dispatch_error,
            result_is_error=lambda _r: False,
        )
        return await self._dispatch_with_recovery(
            org_id=org_id,
            upstream=upstream,
            user_id=user_id,
            session_id=session_id,
            verb=verb,
        )

    async def _resolve_session(
        self,
        org_id: str,
        upstream: UpstreamDefinition,
        user_id: str,
    ) -> _SessionResult:
        """Resolve the MCP session for a tool call.

        Delegates session acquisition (lazy shared reattach for
        service_account; reuse-or-reconnect-from-stored-tokens for OAuth)
        to the shared ``acquire_upstream_session`` so tool calls and the
        dashboard tool-refresh reattach identically. This method only
        resolves the effective user per auth mode and maps an
        acquisition failure back to the right user-facing CallToolResult.
        """
        if upstream.auth.mode == AuthMode.service_account:
            try:
                session = await acquire_upstream_session(
                    org_id=org_id,
                    upstream=upstream,
                    effective_user="",
                    connection_store=self._connection_store,
                    client_manager=self._client_manager,
                    server_url=self._server_url,
                )
            except SessionUnavailable:
                return _SessionResult(
                    error=self._admin_unavailable_error(upstream)
                )
            return _SessionResult(session=session)

        if self._connection_store is None:
            return _SessionResult(
                error=mcp_types.CallToolResult(
                    content=[mcp_types.TextContent(
                        type="text",
                        text=(
                            f"Upstream '{upstream.id}' requires "
                            "OAuth but OAuth is not configured."
                        ),
                    )],
                    isError=True,
                )
            )

        if upstream.auth.mode == AuthMode.admin_oauth:
            owner = await self._resolve_admin_oauth_owner(
                org_id, upstream.id,
            )
            if owner is None:
                return _SessionResult(
                    error=self._admin_unavailable_error(upstream)
                )
            effective_user = owner
        else:
            effective_user = user_id

        # Reuse-or-reconnect-from-stored-tokens — handles token refresh
        # transparently. Never prompts a browser flow.
        try:
            session = await acquire_upstream_session(
                org_id=org_id,
                upstream=upstream,
                effective_user=effective_user,
                connection_store=self._connection_store,
                client_manager=self._client_manager,
                server_url=self._server_url,
            )
        except SessionUnavailable:
            # No stored tokens or connection failed.
            # admin_oauth: only an admin can fix this — point at admins.
            # per_user_oauth: the user can fix it themselves on /my-tools.
            if upstream.auth.mode == AuthMode.admin_oauth:
                return _SessionResult(
                    error=self._admin_unavailable_error(upstream)
                )
            my_tools_url = f"{self._server_url.rstrip('/')}/my-tools"
            return _SessionResult(
                error=mcp_types.CallToolResult(
                    content=[mcp_types.TextContent(
                        type="text",
                        text=(
                            f"You are not signed in to "
                            f"'{upstream.display_name}'. "
                            f"Please tell the user to open {my_tools_url} "
                            f"and click Connect next to "
                            f"'{upstream.display_name}', then retry."
                        ),
                    )],
                    isError=True,
                )
            )

        return _SessionResult(
            session=session,
            auth_identity=(
                f"{upstream.auth.mode.value}:"
                f"{upstream.id}:{effective_user}"
            ),
            effective_user=effective_user,
        )


class _SessionResult:
    """Result of session resolution.

    ``effective_user`` is the per-user session key the session was
    resolved under (the caller for per_user_oauth, the pool owner for
    admin_oauth, ``""`` for service_account's shared session). The
    stall-recovery path needs it to evict the right cached session.
    """

    def __init__(
        self,
        session: Any | None = None,
        error: mcp_types.CallToolResult | None = None,
        auth_identity: str | None = None,
        effective_user: str = "",
    ) -> None:
        self.session = session
        self.error = error
        self.auth_identity = auth_identity
        self.effective_user = effective_user


class UpstreamRouterError(Exception):
    """Raised by ``read_resource`` / ``get_prompt`` when the upstream
    can't be reached for the calling user.

    Carries the user-facing message produced by ``_resolve_session`` so
    the gateway controller can surface it via ``ReadResourceResult`` /
    ``GetPromptResult`` (which lack a CallToolResult-style ``isError``
    bit). The router itself can't return those types directly because
    they each demand a different content shape.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _session_error_text(err: mcp_types.CallToolResult) -> str:
    """Extract the actionable user-facing message from a
    session-unavailable ``CallToolResult``.

    The session-error result is built with a ``TextContent`` first block
    today, so the happy path returns that text. A non-text (or empty)
    first block degrades to a generic message rather than crashing the
    request with an ``AssertionError`` — an internal invariant must never
    leak to the caller as an unhandled exception (ROUTE-1). Shared by
    ``read_resource`` / ``get_prompt``'s ``_on_session_error``."""
    if err.content:
        first = err.content[0]
        if isinstance(first, mcp_types.TextContent):
            return first.text
    return "This upstream is not currently available. Please try again later."
