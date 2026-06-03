"""Shared lifecycle for sandbox / http upstream connection tasks.

Two concrete classes (``HttpConnectionTask`` / ``SandboxConnectionTask``)
used to duplicate identical ``__init__`` fields, ``start``, ``close``,
and ``_run_in_session_context`` bodies. They now inherit from this base
which owns the shared ``__init__`` + all three lifecycle methods.
Per-transport differences ride three small hooks:

- ``_run`` — the per-transport background lifecycle (acquire streams,
  build a ClientSession, set ``self._session_future``, run until
  ``self._shutdown_event`` fires, tear down). The ``except Exception
  → set_exception`` safety net for the future stays inside each
  subclass's ``_run``.
- ``_log_extras() -> dict[str, str]`` — per-transport structlog fields
  attached to spawn-event AND close-timeout log lines. Must include
  ``transport=``; sandbox additionally returns ``provider=``.
- ``_close_timeout_event_name() -> str`` — per-transport event name
  for the close-timeout-cancelling log line. Operators grep on
  ``upstream.{http,stdio,sandbox}.close.timeout_cancelling`` — keeping
  these per-subclass keeps that contract intact.

Subclass ``__init__`` calls ``super().__init__(upstream, user_id)``
to set the shared fields, then sets its own per-transport extras
(``HttpConnectionTask`` adds ``_auth``; ``SandboxConnectionTask``
adds ``_service`` / ``_resources`` / ``_org_id`` / ``_persistence``
etc.).
"""
from __future__ import annotations

import asyncio
import contextvars
import uuid
from abc import ABC, abstractmethod

import structlog
from mcp.client.session import ClientSession

from mcpolis.adapters.upstream_clients.notification_handler import (
    OnPromptListChanged,
    OnResourceListChanged,
    OnToolListChanged,
)
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamDefinition,
    UpstreamSelfDescription,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Default close-grace before cancel(). Subclasses override
# ``_close_timeout_seconds()`` to return their per-module
# ``CLOSE_TIMEOUT`` constant.
DEFAULT_CLOSE_TIMEOUT = 10.0


class ConnectionTaskBase(ABC):
    """Abstract base for upstream-connection tasks. See module docstring."""

    def __init__(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        *,
        bearer_token: str | None = None,
        on_tool_list_changed: OnToolListChanged | None = None,
        on_resource_list_changed: OnResourceListChanged | None = None,
        on_prompt_list_changed: OnPromptListChanged | None = None,
    ) -> None:
        self._upstream = upstream
        self._user_id = user_id
        self._session_id = uuid.uuid4().hex
        self._bearer_token = bearer_token
        self._on_tool_list_changed = on_tool_list_changed
        self._on_resource_list_changed = on_resource_list_changed
        self._on_prompt_list_changed = on_prompt_list_changed
        self._shutdown_event = asyncio.Event()
        self._session_future: asyncio.Future[ClientSession] = (
            asyncio.get_running_loop().create_future()
        )
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.server_info: ServerInfo | None = None
        self.self_description: UpstreamSelfDescription | None = None
        # Set by the transport backend (via the yielded session) when
        # the underlying transport has FATALLY failed — sandbox gone,
        # reattach/stdin send unrecoverable. Distinct from a transient
        # auto-pause the pump reattaches through. ``None`` until a
        # session is established, or for transports that don't track it
        # (treated as always alive). Read by the manager so it
        # reconnects a dead shared session instead of reusing the
        # zombie (whose next send raises ``BrokenResourceError``).
        self._transport_failed: asyncio.Event | None = None

    def is_transport_alive(self) -> bool:
        """Whether this task's transport is still usable.

        ``False`` once the backend has signalled an unrecoverable
        transport failure. Conservatively returns ``True`` when no
        failure signal is wired (HTTP, or before a session exists), so
        existing reuse behaviour is unchanged for those paths.
        """
        return self._transport_failed is None or not self._transport_failed.is_set()

    @abstractmethod
    async def _run(self) -> None:
        """Background lifecycle. See module docstring."""

    @abstractmethod
    def _log_extras(self) -> dict[str, str]:
        """Per-transport structlog fields. See module docstring."""

    @abstractmethod
    def _close_timeout_event_name(self) -> str:
        """Per-transport close-timeout event name. See module docstring."""

    def _close_timeout_seconds(self) -> float:
        """Per-instance close timeout. Subclasses override to return
        the per-adapter ``CLOSE_TIMEOUT`` module constant."""
        return DEFAULT_CLOSE_TIMEOUT

    async def _run_in_session_context(self) -> None:
        """Bind durable session-scoped identifiers, then run the
        connection lifecycle. Paired with the fresh
        ``contextvars.Context()`` in ``start()`` — together they keep
        request-scoped contextvars (``request_id``, etc.) from leaking
        into the long-lived session, and substitute a stable
        ``(upstream_id, user_id, session_id)`` triple so log lines from
        this session remain queryable per-session. See §3.10.

        The spawn event MUST NOT carry a ``request_id`` field — its
        absence is the regression signal for the §3.10 leak. The
        per-transport ``transport=`` (and sandbox's ``provider=``)
        come from ``_log_extras()``.
        """
        structlog.contextvars.bind_contextvars(
            upstream_id=self._upstream.id,
            user_id=self._user_id,
            session_id=self._session_id,
        )
        logger.info("upstream.session.task.spawned", **self._log_extras())
        await self._run()

    async def start(self) -> "ClientSession":
        """Spawn the background task and wait for the session to be
        ready. The task runs in a fresh ``contextvars.Context`` so
        request-scoped contextvars never leak in (see §3.10); inside
        that context, durable identifiers get bound."""
        fresh_ctx = contextvars.Context()
        self._task = asyncio.create_task(
            self._run_in_session_context(), context=fresh_ctx,
        )
        return await self._session_future

    async def close(self) -> None:
        """Signal the background task to shut down and wait for cleanup."""
        if self._closed or self._task is None:
            return
        self._closed = True
        self._shutdown_event.set()
        timeout_seconds = self._close_timeout_seconds()
        try:
            await asyncio.wait_for(self._task, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                self._close_timeout_event_name(),
                upstream_id=self._upstream.id,
                timeout_seconds=timeout_seconds,
                **{k: v for k, v in self._log_extras().items() if k != "transport"},
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
