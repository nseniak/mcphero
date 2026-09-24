"""Shared lifecycle for sandbox / http upstream connection tasks.

Two concrete classes (``HttpConnectionTask`` / ``SandboxConnectionTask``)
used to duplicate identical ``__init__`` fields, ``start``, ``close``,
and ``_run_in_session_context`` bodies. They now inherit from this base
which owns the shared ``__init__`` + all three lifecycle methods.
Per-transport differences ride three small hooks:

- ``_run`` — the per-transport background lifecycle (acquire streams,
  build a ClientSession, hand it over with ``_hand_over``, run until
  ``self._shutdown_event`` fires, tear down). The ``except Exception
  → _fail_start`` safety net stays inside each subclass's ``_run``.
  ``_run`` must also let go at once when the caller of ``start`` gives
  up: it races its handshake through ``_unless_abandoned`` and returns
  when ``_hand_over`` refuses (see ``start``).
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
from collections.abc import Awaitable
from typing import TypeVar

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

# How long a connect whose caller gave up may take to let go of its
# transport. A sandbox that is still being created finishes that first
# (a few seconds), then the session's own cleanup kills it.
ABANDON_TIMEOUT = 30.0

T = TypeVar("T")


class ConnectAbandoned(Exception):
    """Whoever waited for this connect gave up while it was starting."""


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
        session_id: str | None = None,
    ) -> None:
        self._upstream = upstream
        self._user_id = user_id
        # Caller may pre-mint the session id (the sandbox manager does,
        # so the home it substitutes ``${HOME}`` with matches the one
        # ``service.session`` derives from this id). Default to a fresh
        # one for HTTP / tests that don't care.
        self._session_id = session_id or uuid.uuid4().hex
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
        # Set when whoever waited on ``start`` gave up; ``_run`` then lets
        # go of whatever it holds at its next step (see ``start``).
        self._abandoned = asyncio.Event()
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
        that context, durable identifiers get bound.

        If the caller gives up while waiting (a Stop, a shutdown, the
        last request hanging up), the background task is told to let go
        and this returns only once it has: nobody will ever hold or close
        it, and it used to finish starting for nobody, with a sandbox
        running, until the MCP answered or the init timeout (up to
        120 s). That also covers a session that became ready in the very
        step the caller gave up, whose result the cancel threw away.

        The background task is told, never cancelled. Cancelled while it
        creates a sandbox, it would leave one running that only it knows
        about; cancelled while it cleans up, it would skip the kill. So it
        finishes acquiring its transport, then stops at the handshake
        (``_unless_abandoned``) or at the hand-over (``_hand_over``), and
        its context managers release everything on the way out.
        """
        fresh_ctx = contextvars.Context()
        self._task = asyncio.create_task(
            self._run_in_session_context(), context=fresh_ctx,
        )
        try:
            return await self._session_future
        except asyncio.CancelledError:
            await self._abandon_despite_cancels()
            raise

    async def _abandon_despite_cancels(self) -> None:
        """``_abandon``, carried to its end even if this task is cancelled
        again meanwhile (a shutdown, a Stop right after the caller hung
        up). Stopping early would let whoever waits for this connect to
        wind down go on while its transport is still held. Bounded by
        ``ABANDON_TIMEOUT``."""
        abandoning = asyncio.ensure_future(self._abandon())
        while not abandoning.done():
            try:
                await asyncio.shield(abandoning)
            except asyncio.CancelledError:
                continue

    async def _abandon(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        self._closed = True
        self._abandoned.set()
        # A session handed over in the same step is already serving;
        # this ends it the way ``close`` would.
        self._shutdown_event.set()
        done, _ = await asyncio.wait({task}, timeout=ABANDON_TIMEOUT)
        if not done:
            logger.warning(
                "upstream.session.task.abandon_slow",
                upstream_id=self._upstream.id,
                timeout_seconds=ABANDON_TIMEOUT,
                **self._log_extras(),
            )

    def _hand_over(self, session: "ClientSession") -> bool:
        """Give the ready session to ``start``. Returns ``False`` when
        nobody waits for it any more: ``_run`` must then return, so its
        context managers close the session and release the transport."""
        if self._session_future.done():
            return False
        self._session_future.set_result(session)
        return True

    def _fail_start(self, exc: BaseException) -> None:
        """Report a failed start to ``start``, if it still waits."""
        if not self._session_future.done():
            self._session_future.set_exception(exc)

    async def _unless_abandoned(self, work: Awaitable[T]) -> T:
        """Await ``work`` unless the caller of ``start`` gives up first,
        in which case ``work`` is cancelled and ``ConnectAbandoned`` is
        raised. For the handshake, which is where a start spends its
        time (a package download, a slow server)."""
        work_task = asyncio.ensure_future(work)
        gave_up = asyncio.ensure_future(self._abandoned.wait())
        try:
            await asyncio.wait(
                {work_task, gave_up}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for pending in (work_task, gave_up):
                if not pending.done():
                    pending.cancel()
            await asyncio.wait({work_task, gave_up})
        if work_task.cancelled() and self._abandoned.is_set():
            raise ConnectAbandoned()
        return work_task.result()

    async def close(self) -> None:
        """Signal the background task to shut down and wait for cleanup.

        Waits with ``asyncio.wait``, never ``wait_for`` or a bare await:
        those pass a cancel of the CALLER on into the task, whose teardown
        may swallow it (the E2B teardown does, to finish its cleanup), and
        the caller's cancel is then lost. A Stop aimed at a heal that was
        closing the old session went unnoticed that way, and the heal
        brought the upstream back after the Stop. If the caller is
        cancelled here, its cancel propagates and the task goes on
        shutting down by itself.
        """
        if self._closed or self._task is None:
            return
        self._closed = True
        self._shutdown_event.set()
        task = self._task
        timeout_seconds = self._close_timeout_seconds()
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if not done:
            logger.warning(
                self._close_timeout_event_name(),
                upstream_id=self._upstream.id,
                timeout_seconds=timeout_seconds,
                **{k: v for k, v in self._log_extras().items() if k != "transport"},
            )
            task.cancel()
            await asyncio.wait({task})
            return
        if not task.cancelled() and (failure := task.exception()) is not None:
            raise failure
