from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

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
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


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
        upstream = self._upstreams[upstream_id]

        # Merge default arguments
        defaults = upstream.default_arguments.get(original_name, {})
        merged_arguments = {**arguments, **defaults}

        # Resolve session based on auth mode
        auth_identity = f"{upstream.auth.mode.value}:{upstream_id}"

        # A tool call may be retried after a transport stall only if the tool
        # declares itself safe to repeat (idempotent or read-only); a blind
        # retry of a side-effecting tool could double-execute it. Either way a
        # stall on a service_account upstream heals the shared session (drop +
        # fresh reconnect) so the next call lands on a clean transport.
        flags = self._registry.get_tool_annotations(upstream_id, original_name)
        retry_safe = bool(flags.get("readOnly") or flags.get("idempotent"))
        max_attempts = 2 if retry_safe else 1

        start = time.monotonic()
        response_status = "success"
        did_call = False
        try:
            for attempt in range(max_attempts):
                session_result = await self._resolve_session(
                    org_id, upstream, user_id
                )
                if session_result.error is not None:
                    # Session unavailable. On the first attempt no call ever
                    # ran, so return without an audit/analytics row (preserves
                    # the pre-recovery behavior); on a retry it's a failure.
                    if did_call:
                        response_status = "error"
                    return session_result.error
                assert session_result.session is not None
                session = session_result.session
                if session_result.auth_identity:
                    auth_identity = session_result.auth_identity

                did_call = True
                try:
                    result = await session.call_tool(
                        original_name, merged_arguments
                    )
                    if result.isError:
                        response_status = "error"
                    return result
                except Exception as exc:
                    is_last = attempt + 1 >= max_attempts
                    # The E2B post-reattach stdout stall poisons the shared
                    # session: drop it so this retry (and every later call)
                    # runs on a fresh transport. service_account only — OAuth
                    # re-establishes from stored tokens on the next attempt.
                    if (
                        is_transport_stall(exc)
                        and upstream.auth.mode == AuthMode.service_account
                    ):
                        await self._client_manager.reconnect_shared_fresh(
                            upstream
                        )
                    if is_transport_stall(exc) and not is_last:
                        logger.warning(
                            "tool.call.transport_stall_retry",
                            org_id=org_id,
                            upstream_id=upstream_id,
                            tool=prefixed_name,
                            attempt=attempt,
                        )
                        continue
                    response_status = "error"
                    # Don't leak internal exception content (URLs, hostnames,
                    # stack detail, library versions) to MCP clients. Log the
                    # full exception server-side and return an opaque error
                    # with a correlation id the user can quote when asking
                    # an admin to investigate.
                    correlation_id = uuid.uuid4().hex[:12]
                    logger.exception(
                        "tool.call.failed",
                        org_id=org_id,
                        upstream_id=upstream_id,
                        tool=prefixed_name,
                        correlation_id=correlation_id,
                    )
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
            # The loop always returns (success, opaque error, or session
            # error) or continues; it never falls through. Satisfies the
            # type checker that all paths return.
            raise AssertionError("route_call loop exited without returning")
        finally:
            if did_call:
                latency_ms = (time.monotonic() - start) * 1000
                # Per-tool-call observability: latency + outcome per
                # dispatch. Free — every field here is already computed
                # for the audit row + the analytics event below; this
                # just exposes the same payload to the structured-log
                # surface so operators can grep "is upstream X slow"
                # without needing Sentry traces or the audit table.
                logger.info(
                    "upstream.tool_call.completed",
                    org_id=org_id,
                    upstream_id=upstream_id,
                    tool=prefixed_name,
                    response_status=response_status,
                    latency_ms=int(latency_ms),
                    auth_mode=upstream.auth.mode.value,
                    user_id=user_id,
                    session_id=session_id,
                )
                entry = AuditEntry(
                    timestamp=datetime.now(UTC).isoformat(),
                    org_id=org_id,
                    user_id=user_id,
                    upstream_id=upstream_id,
                    auth_mode=upstream.auth.mode.value,
                    auth_identity=auth_identity,
                    tool=prefixed_name,
                    policy_decision="allowed",
                    response_status=response_status,
                    latency_ms=latency_ms,
                    session_id=session_id,
                )
                await self._audit.log(org_id, entry)
                get_analytics().track_async(
                    user_id,
                    "tool_called",
                    {
                        "upstream_id": upstream_id,
                        "tool_name": prefixed_name,
                        "auth_mode": upstream.auth.mode.value,
                        "response_status": response_status,
                        "latency_ms": int(latency_ms),
                        "had_session_id": session_id is not None,
                    },
                )

    async def read_resource(
        self,
        org_id: str,
        upstream_id: str,
        original_uri: str,
        user_id: str,
        session_id: str | None,
    ) -> mcp_types.ReadResourceResult:
        """Forward a ``resources/read`` to the right upstream session.

        Routing mirrors ``route_call``: same ``_resolve_session`` chain
        (so ``per_user_oauth`` upstreams' resources are caller-scoped),
        same audit shape (``tool`` field carries the wrapped resource
        URI for traceability). On a session-not-available outcome the
        helper raises ``UpstreamRouterError`` with the user-facing
        message — the gateway controller maps that to a clean text
        ``ReadResourceResult``.
        """
        upstream = self._upstreams.get(upstream_id)
        if upstream is None:
            raise UpstreamRouterError(f"Unknown upstream '{upstream_id}'.")

        auth_identity = f"{upstream.auth.mode.value}:{upstream_id}"
        session_result = await self._resolve_session(org_id, upstream, user_id)
        if session_result.error is not None:
            text_block = session_result.error.content[0]
            assert isinstance(text_block, mcp_types.TextContent)
            raise UpstreamRouterError(text_block.text)
        assert session_result.session is not None
        session = session_result.session
        if session_result.auth_identity:
            auth_identity = session_result.auth_identity

        start = time.monotonic()
        response_status = "success"
        try:
            result: mcp_types.ReadResourceResult = await session.read_resource(
                AnyUrl(original_uri),
            )
            return result
        except Exception:
            response_status = "error"
            correlation_id = uuid.uuid4().hex[:12]
            logger.exception(
                "resource.read.failed",
                org_id=org_id,
                upstream_id=upstream_id,
                resource_uri=original_uri,
                correlation_id=correlation_id,
            )
            raise UpstreamRouterError(
                f"Upstream resource read failed. Reference: {correlation_id}",
            ) from None
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            entry = AuditEntry(
                timestamp=datetime.now(UTC).isoformat(),
                org_id=org_id,
                user_id=user_id,
                upstream_id=upstream_id,
                auth_mode=upstream.auth.mode.value,
                auth_identity=auth_identity,
                tool=f"resource:{upstream_id}:{original_uri}",
                policy_decision="allowed",
                response_status=response_status,
                latency_ms=latency_ms,
                session_id=session_id,
            )
            await self._audit.log(org_id, entry)

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
        audit semantics. ``arguments`` is forwarded unchanged to the
        upstream, mirroring ``call_tool``'s arguments passthrough.
        """
        upstream = self._upstreams.get(upstream_id)
        if upstream is None:
            raise UpstreamRouterError(f"Unknown upstream '{upstream_id}'.")

        auth_identity = f"{upstream.auth.mode.value}:{upstream_id}"
        session_result = await self._resolve_session(org_id, upstream, user_id)
        if session_result.error is not None:
            text_block = session_result.error.content[0]
            assert isinstance(text_block, mcp_types.TextContent)
            raise UpstreamRouterError(text_block.text)
        assert session_result.session is not None
        session = session_result.session
        if session_result.auth_identity:
            auth_identity = session_result.auth_identity

        start = time.monotonic()
        response_status = "success"
        try:
            result: mcp_types.GetPromptResult = await session.get_prompt(
                original_name, arguments,
            )
            return result
        except Exception:
            response_status = "error"
            correlation_id = uuid.uuid4().hex[:12]
            logger.exception(
                "prompt.get.failed",
                org_id=org_id,
                upstream_id=upstream_id,
                prompt=original_name,
                correlation_id=correlation_id,
            )
            raise UpstreamRouterError(
                f"Upstream prompt failed. Reference: {correlation_id}",
            ) from None
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            entry = AuditEntry(
                timestamp=datetime.now(UTC).isoformat(),
                org_id=org_id,
                user_id=user_id,
                upstream_id=upstream_id,
                auth_mode=upstream.auth.mode.value,
                auth_identity=auth_identity,
                tool=f"prompt:{upstream_id}:{original_name}",
                policy_decision="allowed",
                response_status=response_status,
                latency_ms=latency_ms,
                session_id=session_id,
            )
            await self._audit.log(org_id, entry)

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
        )


class _SessionResult:
    """Result of session resolution."""

    def __init__(
        self,
        session: Any | None = None,
        error: mcp_types.CallToolResult | None = None,
        auth_identity: str | None = None,
    ) -> None:
        self.session = session
        self.error = error
        self.auth_identity = auth_identity


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
