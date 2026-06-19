"""``FakeSandboxService`` — pure in-memory, E2B-shaped ``SandboxService``.

A test double implementing the :class:`SandboxService` Protocol whose
``session()`` is backed by a REAL in-process MCP server (FastMCP's
low-level ``Server.run`` pumped over the same anyio memory-stream pair
shape ``mcp.client.stdio.stdio_client`` produces) — so a wrapping
``ClientSession`` completes a genuine ``initialize`` + ``tools/list`` +
``tools/call`` round-trip. NO HTTP, NO ports, NO subprocesses: the
server runs as an asyncio task sharing the test's event loop, which
keeps it xdist / parallel-safe.

It exposes deterministic control knobs so router/manager recovery
tests can drive the exact failure shapes the production stall-recovery
paths classify:

- :meth:`SessionHandle.stall` — the server stops pumping its streams
  WITHOUT closing them (no exception, no response). A wrapping
  ``ClientSession`` then hangs on the next request, exactly like the
  E2B #1128 post-reattach silent stall that ``dispatch_with_liveness``
  bounds with a liveness ping.
- :meth:`SessionHandle.kill` — set ``SandboxSession.transport_failed``
  and close the read side, so the next client send raises
  ``BrokenResourceError`` (the zombie-session case the manager's
  ``is_transport_alive`` gate reconnects through).
- :meth:`SessionHandle.fire_exit` — resolve the per-session
  ``ExitSignal`` to simulate the subprocess dying during / after init
  (drives the ``SubprocessExitedDuringInit`` fast-fail race).

Plus a service-level ``session_open_count`` counter so single-flight /
coalescing tests can assert "exactly one fresh session was opened".

Build one with :func:`make_fake_sandbox_service`.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TextIO

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from mcpolis.adapters.sandbox_services.exit_signal import ExitSignalImpl
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    ProviderExitInfo,
    ResourcesUnsupported,
    SandboxCapabilities,
    SandboxProviderName,
    SandboxResourceCombo,
    SandboxResources,
    SandboxSession,
    SnapshotRef,
)

# E2B-shaped capability grid: paired CPU/RAM templates, disk fixed at
# template-build time (empty axis), pause/resume supported. Mirrors the
# real E2B backend's wire shape closely enough that the contract suite's
# capability + validation scenarios exercise an E2B-shaped surface,
# while staying a self-contained constant the fake fully controls.
_CPU_RAM_PAIRS: tuple[tuple[float, int], ...] = (
    (1.0, 1024),
    (2.0, 2048),
    (2.0, 4096),
    (4.0, 4096),
    (4.0, 8192),
)
_ALLOWED_CPU_VCPUS: tuple[float, ...] = (1.0, 2.0, 4.0)
_ALLOWED_MEMORY_MB: tuple[int, ...] = (1024, 2048, 4096, 8192)


ServerFactory = Callable[[], FastMCP]


def _default_server_factory() -> FastMCP:
    """An echo MCP server with one ``echo`` tool — enough to prove a
    real ``initialize`` + ``tools/list`` + ``tools/call`` round-trip."""
    server = FastMCP(name="FakeUpstream")

    @server.tool(name="echo", description="Echo back the message")
    def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"echo:{message}"

    return server


class SessionHandle:
    """Per-session control surface returned to tests via
    :attr:`FakeSandboxService.last_session` (and the
    :attr:`FakeSandboxService.sessions` list).

    Carries the knobs that flip a live session into each failure mode.
    All knobs are safe to call once the session context has been
    entered; calling after the context has exited is a harmless no-op.
    """

    def __init__(
        self,
        *,
        session_id: str,
        exit_signal: ExitSignalImpl,
        transport_failed: asyncio.Event,
        stall_gate: asyncio.Event,
    ) -> None:
        self.session_id = session_id
        self._exit_signal = exit_signal
        self._transport_failed = transport_failed
        # The client→server relay forwards while ``stall_gate`` is SET.
        # ``stall()`` clears it, so the relay holds the next request and
        # the server never sees it — a silent stall with streams open.
        self._stall_gate = stall_gate
        self._stall_gate.set()
        # Set via ``_attach_runtime`` once the relay/pump tasks + the
        # server→client send stream exist. ``None`` before entry.
        self._server_task: asyncio.Task[None] | None = None
        self._relay_task: asyncio.Task[None] | None = None
        self._to_client_send: (
            MemoryObjectSendStream[SessionMessage | Exception] | None
        ) = None

    def bind_runtime(
        self,
        *,
        relay_task: asyncio.Task[None],
        server_task: asyncio.Task[None],
        to_client_send: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        """Wire the live transport tasks/streams onto the handle once the
        session context has opened them (called by ``_session_cm``).

        Internal: tests should use the knobs (:meth:`stall`, :meth:`kill`,
        :meth:`fire_exit`), not this."""
        self._relay_task = relay_task
        self._server_task = server_task
        self._to_client_send = to_client_send

    @property
    def transport_failed(self) -> asyncio.Event:
        """The session's fatal-transport event (mirrors
        ``SandboxSession.transport_failed``)."""
        return self._transport_failed

    @property
    def is_alive(self) -> bool:
        """``True`` until :meth:`kill` (or the backend) has flagged the
        transport as fatally failed."""
        return not self._transport_failed.is_set()

    def stall(self) -> None:
        """Make the transport go SILENT while leaving every stream OPEN.

        Clears the relay gate so the client→server relay stops
        forwarding: the server never sees the next request and never
        responds, so a wrapping ``ClientSession`` hangs with no
        exception and no result — the E2B #1128 silent-stall shape the
        dispatch-path liveness ping is built to detect. Idempotent.

        Deliberately does NOT cancel ``low.run`` or close any stream:
        cancelling the server tears down its anyio task group, which
        closes the streams and turns the next send into a
        ``BrokenResourceError`` — that's the :meth:`kill` shape, not a
        silent stall."""
        self._stall_gate.clear()

    def resume(self) -> None:
        """Undo :meth:`stall` — let the relay forward queued/next
        requests again. Lets a test prove the SAME session recovers when
        the peer comes back, not only that a fresh session does."""
        self._stall_gate.set()

    def kill(self) -> None:
        """Simulate a dead sandbox (the zombie-session case). Sets
        ``transport_failed`` — the gate the manager reads via
        ``is_transport_alive()`` to decide a shared session is dead and
        reconnect it — then closes the server→client stream (so the
        wrapping ``ClientSession``'s reader sees end-of-stream) and tears
        down the relay + server pump (so the client→server path no longer
        reaches a live server). ``transport_failed`` is the load-bearing
        signal; the stream teardown is what makes an in-flight call stop.
        Idempotent."""
        self._transport_failed.set()
        # Unblock a stalled relay so its cancellation isn't parked on the
        # gate, then cancel both tasks.
        self._stall_gate.set()
        for task in (self._relay_task, self._server_task):
            if task is not None and not task.done():
                task.cancel()
        if self._to_client_send is not None:
            self._to_client_send.close()

    def fire_exit(self, exit_code: int | None = 1, stderr_tail: str = "") -> None:
        """Resolve the per-session ``ExitSignal`` to simulate the
        sandboxed process dying. Drives the
        ``SubprocessExitedDuringInit`` fast-fail race when fired during
        the init window. Idempotent (only the first exit observation
        wins, per ``ExitSignalImpl``)."""
        if stderr_tail:
            self._exit_signal.append_stderr(stderr_tail.encode("utf-8"))
        self._exit_signal.mark_exited(exit_code)


class FakeSandboxService:
    """In-memory ``SandboxService`` whose sessions run a real MCP server.

    ``name = "e2b"`` so it stands in for the production backend in
    shape-sensitive code paths (provider lookups, capability grids,
    persistence-ref provider tags). Construct via
    :func:`make_fake_sandbox_service`.
    """

    name: SandboxProviderName = "e2b"

    def __init__(
        self,
        *,
        server_factory: ServerFactory | None = None,
        supports_pause_resume: bool = True,
    ) -> None:
        self._server_factory = server_factory or _default_server_factory
        self._supports_pause_resume = supports_pause_resume
        # Number of times ``session()``'s context manager has been
        # ENTERED. Single-flight / coalescing tests assert this is
        # exactly 1 after N concurrent reconnect attempts.
        self.session_open_count = 0
        # Every handle ever opened, newest last. ``last_session`` is the
        # common accessor; ``sessions`` is for tests that open several.
        self.sessions: list[SessionHandle] = []

    @property
    def last_session(self) -> SessionHandle | None:
        return self.sessions[-1] if self.sessions else None

    # ---------- capabilities + validation ----------

    def capabilities(self) -> SandboxCapabilities:
        combos = tuple(
            SandboxResourceCombo(cpu_vcpus=cpu, memory_mb=ram, disk_gb=0)
            for (cpu, ram) in _CPU_RAM_PAIRS
        )
        return SandboxCapabilities(
            provider="e2b",
            allowed_cpu_vcpus=_ALLOWED_CPU_VCPUS,
            allowed_memory_mb=_ALLOWED_MEMORY_MB,
            allowed_disk_gb=(),
            allowed_combinations=combos,
            supports_pause_resume=self._supports_pause_resume,
            supports_egress_filtering=False,
            supports_persistent_disk=False,
        )

    def validate_resources(self, resources: SandboxResources) -> None:
        if resources.cpu_vcpus not in _ALLOWED_CPU_VCPUS:
            raise ResourcesUnsupported(
                "cpu_vcpus", resources.cpu_vcpus, allowed=_ALLOWED_CPU_VCPUS,
            )
        if resources.memory_mb not in _ALLOWED_MEMORY_MB:
            raise ResourcesUnsupported(
                "memory_mb", resources.memory_mb, allowed=_ALLOWED_MEMORY_MB,
            )
        if (resources.cpu_vcpus, resources.memory_mb) not in _CPU_RAM_PAIRS:
            raise ResourcesUnsupported(
                "memory_mb", resources.memory_mb, allowed=_ALLOWED_MEMORY_MB,
            )

    def sandbox_home(self, *, session_id: str) -> str:
        # E2B's container home is fixed at ``/home/user`` regardless of
        # session; mirror that so ``${HOME}`` substitution under the
        # fake matches the real backend's contract.
        _ = session_id
        return "/home/user"

    # ---------- session ----------

    def session(
        self,
        *,
        session_id: str,
        org_id: str,
        upstream: UpstreamDefinition,
        resources: SandboxResources,
        denylist: Sequence[str],
        resume_from: SnapshotRef | None = None,
        errlog: TextIO | None = None,
        extra_env: dict[str, str] | None = None,
        materialize_files: Sequence[MaterializeFile] | None = None,
    ) -> AbstractAsyncContextManager[SandboxSession]:
        _ = (
            org_id, upstream, resources, denylist, errlog, extra_env,
            materialize_files,
        )
        if resume_from is not None and resume_from.provider != self.name:
            raise ValueError(
                f"snapshot belongs to provider {resume_from.provider!r}, "
                f"not {self.name!r}",
            )
        return self._session_cm(session_id=session_id)

    @asynccontextmanager
    async def _session_cm(
        self, *, session_id: str,
    ) -> AsyncIterator[SandboxSession]:
        # THREE memory-stream pairs. The client-facing pair matches
        # ``mcp.client.stdio.stdio_client``'s output shape; a relay sits
        # on the client→server direction so ``stall()`` can stop
        # forwarding requests into the server while every stream stays
        # open (a true SILENT stall — no exception, no response).
        #
        #   to_client     : server→client   (ClientSession reads)
        #   from_client   : client→relay    (ClientSession writes)
        #   server_in     : relay→server    (low.run reads)
        to_client_send, to_client_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        from_client_send, from_client_recv = anyio.create_memory_object_stream[
            SessionMessage
        ](0)
        server_in_send, server_in_recv = anyio.create_memory_object_stream[
            SessionMessage
        ](0)

        exit_signal = ExitSignalImpl()
        transport_failed: asyncio.Event = asyncio.Event()
        stall_gate: asyncio.Event = asyncio.Event()
        handle = SessionHandle(
            session_id=session_id,
            exit_signal=exit_signal,
            transport_failed=transport_failed,
            stall_gate=stall_gate,
        )

        server = self._server_factory()
        low = server._mcp_server  # pyright: ignore[reportPrivateUsage]
        init_options = low.create_initialization_options()

        cancelled_exc = anyio.get_cancelled_exc_class()

        async def _relay() -> None:
            # Forward client→server requests, but only while the gate is
            # SET. ``stall()`` clears it, so this awaits mid-stream with
            # the next request undelivered — the server stays silent and
            # the streams stay open. ``resume()`` re-sets the gate.
            try:
                async for message in from_client_recv:
                    await stall_gate.wait()
                    await server_in_send.send(message)
            except (cancelled_exc, asyncio.CancelledError):
                raise
            except Exception:
                # Stream closed on teardown / kill — stop relaying.
                pass
            finally:
                server_in_send.close()

        async def _pump() -> None:
            # The low-level server reads the relayed ``server_in_recv``
            # and writes ``to_client_send`` — the SAME stream shape a
            # real stdio backend produces.
            try:
                await low.run(
                    server_in_recv,
                    to_client_send,
                    init_options,
                    raise_exceptions=False,
                )
            except (cancelled_exc, asyncio.CancelledError):
                raise
            except Exception:
                # A torn-down stream surfaces here on close; swallow so
                # the pump task never raises into the gather on teardown.
                pass

        relay_task: asyncio.Task[None] = asyncio.create_task(_relay())
        server_task: asyncio.Task[None] = asyncio.create_task(_pump())
        handle.bind_runtime(
            relay_task=relay_task,
            server_task=server_task,
            to_client_send=to_client_send,
        )

        self.session_open_count += 1
        self.sessions.append(handle)

        try:
            yield SandboxSession(
                read_stream=to_client_recv,
                write_stream=from_client_send,
                exit_signal=exit_signal,
                transport_failed=transport_failed,
            )
        finally:
            # Release a stalled relay so its cancel isn't parked on the
            # gate, then tear down both tasks + every stream end.
            stall_gate.set()
            for task in (relay_task, server_task):
                if not task.done():
                    task.cancel()
            for task in (relay_task, server_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for stream in (
                to_client_send, to_client_recv,
                from_client_send, from_client_recv,
                server_in_send, server_in_recv,
            ):
                stream.close()

    # ---------- pause / persistence (mostly no-op for the fake) ----------

    async def pause(self, session_id: str) -> SnapshotRef | None:
        # The fake never registers a live session for pause, so the
        # cross-backend "unknown session → None" contract holds. A
        # backend that can pause MUST return non-None for a live
        # registered session; the fake registers none, so None is
        # always correct.
        _ = session_id
        return None

    async def on_upstream_removed(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        _ = org_id, upstream_id

    async def kill_persisted_session(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        _ = org_id, upstream_id

    def map_exit(
        self, raw: ProviderExitInfo,
    ) -> tuple[ExitReason, str | None]:
        # Coarse-signal mapping mirroring the documented PROVIDER_ERROR
        # fallback: any populated message becomes the detail string.
        detail = raw.raw_message or None
        if raw.exit_code is not None and raw.exit_code != 0:
            return ExitReason.SUBPROCESS_EXITED, detail
        return ExitReason.PROVIDER_ERROR, detail or "fake provider exit"


def make_fake_sandbox_service(
    *,
    server_factory: ServerFactory | None = None,
    supports_pause_resume: bool = True,
) -> FakeSandboxService:
    """Build a :class:`FakeSandboxService`.

    ``server_factory`` overrides the per-session MCP server (default: a
    one-tool ``echo`` server). It's called once per ``session()`` open,
    so each session gets a fresh server instance. ``supports_pause_resume``
    flips the declared capability for tests that need the no-pause shape.
    """
    return FakeSandboxService(
        server_factory=server_factory,
        supports_pause_resume=supports_pause_resume,
    )


__all__ = [
    "FakeSandboxService",
    "SessionHandle",
    "make_fake_sandbox_service",
]
