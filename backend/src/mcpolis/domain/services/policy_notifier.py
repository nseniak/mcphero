"""Debounced tool-list-change notifications for MCP gateway sessions."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

import mcp.types as mcp_types
import structlog
from mcp.shared.message import SessionMessage

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.services.upstream_connection_service import (
    acquire_and_refresh_with_recovery,
)

if TYPE_CHECKING:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from mcpolis.adapters.repositories.connection_store import ConnectionStore
    from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
    from mcpolis.domain.services.tool_registry import ToolRegistry

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_TOOL_LIST_CHANGED_NOTIFICATION = mcp_types.JSONRPCNotification(
    jsonrpc="2.0",
    method="notifications/tools/list_changed",
)

_RESOURCE_LIST_CHANGED_NOTIFICATION = mcp_types.JSONRPCNotification(
    jsonrpc="2.0",
    method="notifications/resources/list_changed",
)

_PROMPT_LIST_CHANGED_NOTIFICATION = mcp_types.JSONRPCNotification(
    jsonrpc="2.0",
    method="notifications/prompts/list_changed",
)


class PolicyNotifier:
    """Sends debounced ``notifications/tools/list_changed`` to affected gateway sessions.

    After a policy change, the notifier waits *debounce_seconds* before
    actually pushing the notification.  If another change for the same
    debounce key arrives within that window the timer resets, so rapid
    admin edits collapse into a single notification per burst.
    """

    def __init__(
        self,
        session_manager: StreamableHTTPSessionManager,
        session_registry: GatewaySessionRegistry,
        runtime_manager: OrgRuntimeManager,
        connection_store: "ConnectionStore | None" = None,
        server_url: str = "",
        debounce_seconds: float = 10.0,
    ) -> None:
        self._session_manager = session_manager
        self._registry = session_registry
        self._runtime_manager = runtime_manager
        # Threaded through to ``acquire_and_refresh_with_recovery`` so a
        # ``tools/list_changed``-triggered refresh can heal a stalled
        # service_account session the same way the dashboard refresh does.
        self._connection_store = connection_store
        self._server_url = server_url
        self._debounce_seconds = debounce_seconds
        self._pending: dict[str, asyncio.TimerHandle] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_role_changed(self, org_id: str, role_name: str) -> None:
        """Schedule a debounced notification for all users with *role_name*."""
        key = f"role:{org_id}:{role_name}"
        self._schedule(key, lambda: self._send_for_role(org_id, role_name))

    def notify_user_changed(self, org_id: str, user_id: str) -> None:
        """Schedule a debounced notification for a specific user."""
        key = f"user:{org_id}:{user_id}"
        self._schedule(key, lambda: self._send_for_user(org_id, user_id))

    def notify_all_roles(self) -> None:
        """Schedule a debounced broadcast to all connected sessions."""
        key = "broadcast"
        self._schedule(key, self._send_broadcast)

    def notify_upstream_tools_changed(
        self, org_id: str, upstream_id: str,
    ) -> None:
        """Debounce-refresh an upstream's tools and notify the org.

        Invoked when an upstream MCP server sends
        ``notifications/tools/list_changed``. Refreshes the gateway's
        tool cache for that upstream and pushes the same notification
        to every active gateway session in the org.
        """
        key = f"upstream_tools:{org_id}:{upstream_id}"
        self._schedule(
            key,
            lambda: self._refresh_and_broadcast(org_id, upstream_id),
        )

    def notify_upstream_resources_changed(
        self, org_id: str, upstream_id: str,
    ) -> None:
        """Debounce-refresh resources for *upstream_id* and notify the org.

        Counterpart to ``notify_upstream_tools_changed``. Wired in
        ``OrgRuntimeManager.set_on_upstream_resources_changed`` so the
        upstream's ``ResourceListChangedNotification`` round-trips
        through the same debounce machinery.
        """
        key = f"upstream_resources:{org_id}:{upstream_id}"
        self._schedule(
            key,
            lambda: self._refresh_resources_and_broadcast(org_id, upstream_id),
        )

    def notify_upstream_prompts_changed(
        self, org_id: str, upstream_id: str,
    ) -> None:
        key = f"upstream_prompts:{org_id}:{upstream_id}"
        self._schedule(
            key,
            lambda: self._refresh_prompts_and_broadcast(org_id, upstream_id),
        )

    def terminate_user_sessions(self, org_id: str, user_id: str) -> int:
        """Immediately terminate all gateway sessions for a user.

        Removes sessions from the SDK's internal tracking so subsequent
        requests with the same session ID receive a 404, forcing the
        client to re-initialise (which will fail if tokens are revoked).
        """
        session_ids = self._registry.get_session_ids_for_user(org_id, user_id)
        if not session_ids:
            return 0

        instances = self._session_manager._server_instances  # pyright: ignore[reportPrivateUsage]
        removed = 0
        for sid in session_ids:
            transport = instances.pop(sid, None)
            if transport is not None:
                removed += 1
            self._registry.unregister(sid)

        if removed:
            logger.info(
                "session.terminated",
                removed_count=removed,
                user=user_id,
                org_id=org_id,
            )
        return removed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schedule(self, key: str, callback: Callable[[], None]) -> None:
        """(Re)schedule a debounced callback under *key*."""
        existing = self._pending.pop(key, None)
        if existing is not None:
            existing.cancel()

        loop = asyncio.get_running_loop()

        def _fire() -> None:
            self._pending.pop(key, None)
            callback()

        handle = loop.call_later(self._debounce_seconds, _fire)
        self._pending[key] = handle

    def _send_for_role(self, org_id: str, role_name: str) -> None:
        """Resolve users with *role_name* and notify their sessions."""
        runtime = self._runtime_manager.get_cached(org_id)
        if runtime is None:
            return
        config = runtime.policy_engine.config
        user_ids = [
            uid for uid, udef in config.users.items()
            if udef.role == role_name
        ]
        session_ids: list[str] = []
        for uid in user_ids:
            session_ids.extend(
                self._registry.get_session_ids_for_user(org_id, uid)
            )
        self._send_to_sessions(session_ids)

    def _send_for_user(self, org_id: str, user_id: str) -> None:
        """Notify all sessions for a specific user."""
        session_ids = self._registry.get_session_ids_for_user(org_id, user_id)
        self._send_to_sessions(session_ids)

    def _send_broadcast(self) -> None:
        """Notify all connected sessions."""
        session_ids = self._registry.get_all_session_ids()
        self._send_to_sessions(session_ids)

    def _refresh_and_broadcast(self, org_id: str, upstream_id: str) -> None:
        """Schedule async refresh of *upstream_id*'s tools and broadcast to org.

        Must run on the event loop (debounce timer fires there), so the
        async refresh is dispatched as a task.
        """
        runtime = self._runtime_manager.get_cached(org_id)
        if runtime is None:
            return
        asyncio.create_task(
            self._refresh_upstream_and_notify(
                runtime, org_id, upstream_id,
            ),
        )

    def _refresh_resources_and_broadcast(
        self, org_id: str, upstream_id: str,
    ) -> None:
        runtime = self._runtime_manager.get_cached(org_id)
        if runtime is None:
            return
        asyncio.create_task(
            self._refresh_upstream_resources_and_notify(
                runtime.tool_registry, org_id, upstream_id,
            ),
        )

    def _refresh_prompts_and_broadcast(
        self, org_id: str, upstream_id: str,
    ) -> None:
        runtime = self._runtime_manager.get_cached(org_id)
        if runtime is None:
            return
        asyncio.create_task(
            self._refresh_upstream_prompts_and_notify(
                runtime.tool_registry, org_id, upstream_id,
            ),
        )

    def _user_ids_for_org(self, org_id: str) -> set[str]:
        """Return every email configured under *org_id*'s policy.

        Used to widen ``get_session_ids_for_org`` so a multi-org cloud
        gateway session (registered under ``MULTI_ORG_SENTINEL``) is
        still notified when one of its member-orgs' tool list changes.
        """
        runtime = self._runtime_manager.get_cached(org_id)
        if runtime is None:
            return set()
        return set(runtime.policy_engine.config.users.keys())

    async def _refresh_upstream_and_notify(
        self,
        runtime: "OrgRuntime",
        org_id: str,
        upstream_id: str,
    ) -> None:
        upstream = next(
            (u for u in runtime.upstreams if u.id == upstream_id), None,
        )
        try:
            if (
                upstream is not None
                and upstream.auth.mode == AuthMode.service_account
            ):
                # A ``tools/list_changed`` from a service_account upstream can
                # land on an E2B session whose stdout stalled post-reattach; a
                # raw refresh then times out and the session stays poisoned
                # (MCPOLIS-BACKEND-P). Route through the same recovery the
                # dashboard refresh uses: drop the stalled session, reconnect
                # FRESH, and retry. Other auth modes keep the direct refresh —
                # the stall is a shared-session phenomenon and
                # ``reconnect_shared_fresh`` is service_account-only.
                await acquire_and_refresh_with_recovery(
                    org_id=org_id,
                    upstream=upstream,
                    effective_user="",
                    connection_store=self._connection_store,
                    client_manager=runtime.client_manager,
                    tool_registry=runtime.tool_registry,
                    server_url=self._server_url,
                )
            else:
                await runtime.tool_registry.refresh_upstream(upstream_id)
        except KeyError:
            # Race between an upstream-emitted ``tools/list_changed``
            # and a Stop / Reconnect: by the time the debounced
            # refresh fires, the shared session is gone. The
            # ``tools/list_changed`` we'd been reacting to is now
            # stale. No session to refresh, no panic — just skip.
            logger.debug(
                "tool.registry.refresh_after_change.session_gone",
                upstream_id=upstream_id, org_id=org_id,
            )
        except Exception as exc:
            logger.exception(
                "tool.registry.refresh_after_change.failed",
                upstream_id=upstream_id,
                org_id=org_id,
                error=str(exc),
                error_class=type(exc).__name__,
            )
        session_ids = self._registry.get_session_ids_for_org(
            org_id, user_ids_in_org=self._user_ids_for_org(org_id),
        )
        self._send_to_sessions(session_ids)

    async def _refresh_upstream_resources_and_notify(
        self,
        tool_registry: "ToolRegistry",
        org_id: str,
        upstream_id: str,
    ) -> None:
        try:
            await tool_registry.refresh_resources_for_upstream(upstream_id)
        except KeyError:
            logger.debug(
                "resource.registry.refresh_after_change.session_gone",
                upstream_id=upstream_id, org_id=org_id,
            )
        except Exception as exc:
            logger.exception(
                "resource.registry.refresh_after_change.failed",
                upstream_id=upstream_id,
                org_id=org_id,
                error=str(exc),
                error_class=type(exc).__name__,
            )
        session_ids = self._registry.get_session_ids_for_org(
            org_id, user_ids_in_org=self._user_ids_for_org(org_id),
        )
        self._send_to_sessions(
            session_ids,
            notification=_RESOURCE_LIST_CHANGED_NOTIFICATION,
            log_event="resource.list_changed.notification.sent",
        )

    async def _refresh_upstream_prompts_and_notify(
        self,
        tool_registry: "ToolRegistry",
        org_id: str,
        upstream_id: str,
    ) -> None:
        try:
            await tool_registry.refresh_prompts_for_upstream(upstream_id)
        except KeyError:
            logger.debug(
                "prompt.registry.refresh_after_change.session_gone",
                upstream_id=upstream_id, org_id=org_id,
            )
        except Exception as exc:
            logger.exception(
                "prompt.registry.refresh_after_change.failed",
                upstream_id=upstream_id,
                org_id=org_id,
                error=str(exc),
                error_class=type(exc).__name__,
            )
        session_ids = self._registry.get_session_ids_for_org(
            org_id, user_ids_in_org=self._user_ids_for_org(org_id),
        )
        self._send_to_sessions(
            session_ids,
            notification=_PROMPT_LIST_CHANGED_NOTIFICATION,
            log_event="prompt.list_changed.notification.sent",
        )

    def _send_to_sessions(
        self,
        session_ids: list[str],
        *,
        notification: mcp_types.JSONRPCNotification = (
            _TOOL_LIST_CHANGED_NOTIFICATION
        ),
        log_event: str = "tool.list_changed.notification.sent",
    ) -> None:
        """Push *notification* into each live session transport.

        Defaults to the tool-list-changed notification so existing
        tools-only callers (``_send_for_role``, ``_send_for_user``,
        ``_send_broadcast``) keep working unchanged. Resource / prompt
        callers override *notification* and *log_event*.
        """
        if not session_ids:
            return

        # Access internal session tracking — no public API in the MCP SDK
        instances = self._session_manager._server_instances  # pyright: ignore[reportPrivateUsage]
        msg = SessionMessage(
            message=mcp_types.JSONRPCMessage(root=notification),
        )

        sent = 0
        for sid in session_ids:
            transport = instances.get(sid)
            if transport is None:
                # Session gone — clean up registry
                self._registry.unregister(sid)
                continue
            writer = transport._write_stream  # pyright: ignore[reportPrivateUsage]
            if writer is None:
                continue
            try:
                writer.send_nowait(msg)
                sent += 1
            except Exception:
                logger.debug(
                    "list_changed.notification.failed",
                    session_id=sid,
                    method=notification.method,
                )

        if sent:
            logger.info(log_event, session_count=sent)
