from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, TextIO, cast

import structlog
from mcp.client.session import ClientSession

from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_service import (
    ExitSignal,
    MaterializeFile,
    SandboxResources,
    SandboxService,
    SnapshotRef,
)
from mcpolis.adapters.upstream_clients.connection_task_base import (
    ConnectionTaskBase,
)
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.adapters.upstream_clients.notification_handler import (
    OnPromptListChanged,
    OnResourceListChanged,
    OnToolListChanged,
    build_tool_change_message_handler,
)
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamDefinition,
    UpstreamSelfDescription,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# INIT_TIMEOUT covers MCP `initialize` round-trip. For *local* stdio
# this fires within seconds; for *remote* stdio (runner-hosted
# sandbox) the runner has to spin up a fresh podman container and
# the entrypoint typically runs `npx -y <pkg>` or `uvx <pkg>`, which
# does a cold download from npm/PyPI before the MCP server even
# starts. Empirically @modelcontextprotocol/server-everything cold
# pull + spawn lands around 30–35s; 120s leaves ample headroom for
# slow networks or larger packages. Genuinely-broken upstreams should
# be caught much sooner via process-exit polling, not by waiting out
# this timeout.
INIT_TIMEOUT = 120.0
CLOSE_TIMEOUT = 10.0


class SubprocessExitedDuringInit(Exception):
    """Raised when the sandboxed MCP process exits before completing
    the JSON-RPC ``initialize`` handshake.

    Surfaced by the race in ``SandboxConnectionTask._run`` between
    ``session.initialize()`` and ``ExitSignal.wait()``. Lets the admin
    UI report a fast, specific failure (with the exit code) on bogus
    commands like ``python3 bogus`` instead of waiting out the full
    ``INIT_TIMEOUT``. The captured stderr tail is preserved on
    ``stderr_tail`` for logs / "Sandbox logs" panel use, but kept out
    of the exception message so the error banner doesn't echo raw
    process output. Any exit during the init window — including a
    clean ``code=0`` exit — qualifies, since an MCP server is supposed
    to stay alive past initialize().
    """

    def __init__(self, exit_code: int | None, stderr_tail: str) -> None:
        # ``exit_code=None`` means the watcher observed the process is
        # gone but couldn't read a code (e.g. ``process.wait()`` raised).
        # Render that as ``unknown`` so the operator still sees a
        # "process is gone" message rather than a confusing ``None``.
        code_repr = "unknown" if exit_code is None else str(exit_code)
        super().__init__(
            f"Process exited with code {code_repr} before completing"
            " MCP handshake. See the Sandbox logs for stderr output.",
        )
        self.exit_code: int | None = exit_code
        self.stderr_tail: str = stderr_tail


class StdioInitTimeout(Exception):
    """Raised when an stdio MCP process is alive but does not complete
    the JSON-RPC ``initialize`` handshake within ``INIT_TIMEOUT``.

    The dominant cause is an stdio server that opens a browser to
    authenticate (``@modelcontextprotocol/server-gdrive``-style):
    ``webbrowser.open()`` no-ops in the cloud sandbox, the localhost
    callback can't be reached, and the server sits forever waiting on
    a code that won't arrive. Reshape the generic
    :class:`asyncio.TimeoutError` from the race in
    :func:`init_with_exit_race` into this diagnostic so the operator
    sees the supported alternatives in the dashboard instead of a
    bare "did not complete" message. Rides the same surfacing path as
    :class:`SubprocessExitedDuringInit`.
    """

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(
            f"MCP server did not initialise within {int(timeout_seconds)}s."
            " The most common cause is an stdio server that opens a"
            " browser to authenticate — that pattern is not supported"
            " in the cloud sandbox. Configure credentials via Variables"
            " (env vars) or Files (uploaded credential / config files),"
            " or use the streamable-HTTP variant of this MCP if"
            " available.",
        )
        self.timeout_seconds: float = timeout_seconds


async def init_with_exit_race(
    session: ClientSession,
    exit_signal: ExitSignal,
    *,
    timeout: float = INIT_TIMEOUT,
) -> Any:
    """Race ``session.initialize()`` against the per-session exit
    signal so a subprocess that dies during the init window (bogus
    command, immediate crash, premature ``sys.exit(0)``) surfaces as
    a fast :class:`SubprocessExitedDuringInit` rather than waiting
    out the full ``timeout``.

    Three terminal outcomes:
    1. ``init`` completes first → return its result (may also
       re-raise an SDK error from the handshake).
    2. ``exit`` fires first → raise
       :class:`SubprocessExitedDuringInit` with the captured exit
       code + stderr tail.
    3. neither fires before ``timeout`` → raise
       :class:`StdioInitTimeout` with a message pointing operators
       at the supported authentication alternatives (the typical
       hang cause is an stdio server waiting on a browser-OAuth
       callback that can't arrive in the sandbox).

    Module-level (rather than a method on ``SandboxConnectionTask``)
    so unit tests can drive it with stub ``session`` / ``exit_signal``
    objects without constructing a full task graph.
    """
    init_task: asyncio.Task[Any] = asyncio.create_task(
        session.initialize(),
    )
    exit_task: asyncio.Task[None] = asyncio.create_task(
        exit_signal.wait(),
    )
    try:
        done, _pending = await asyncio.wait(
            {init_task, exit_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (init_task, exit_task):
            if not t.done():
                t.cancel()
        # Drain cancelled / completed tasks so neither leaks an
        # "asyncio Task was destroyed but it is pending" warning nor
        # an unawaited exception. ``BaseException`` covers
        # ``CancelledError`` cleanly without a bare-except lint trip.
        for t in (init_task, exit_task):
            with contextlib.suppress(BaseException):
                await t

    if not done:
        raise StdioInitTimeout(timeout)
    if exit_task in done and init_task not in done:
        snap = exit_signal.snapshot()
        raise SubprocessExitedDuringInit(snap.exit_code, snap.stderr_tail)
    # init_task is in ``done``: surface its result, which also
    # re-raises any SDK exception caught during initialize().
    return init_task.result()


DenylistLoader = Callable[[], Awaitable[list[str]]]


class SandboxConnectionTask(ConnectionTaskBase):
    """Unified ``ClientSession`` task driven by a :class:`SandboxService`.

    The same task drives every backend, picking up a stream pair via
    ``service.session()`` and layering ``ClientSession`` + initialize
    + the message handlers on top.
    """

    def __init__(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        service: SandboxService,
        resources: SandboxResources,
        org_id: str = "default",
        bearer_token: str | None = None,
        log_buffer: LogBuffer | None = None,
        denylist_loader: "DenylistLoader | None" = None,
        on_tool_list_changed: OnToolListChanged | None = None,
        on_resource_list_changed: OnResourceListChanged | None = None,
        on_prompt_list_changed: OnPromptListChanged | None = None,
        sandbox_persistence: "SandboxPersistenceRepository | None" = None,
        mcpolis_instance: str | None = None,
        materialize_files: "list[MaterializeFile] | None" = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(
            upstream,
            user_id,
            bearer_token=bearer_token,
            on_tool_list_changed=on_tool_list_changed,
            on_resource_list_changed=on_resource_list_changed,
            on_prompt_list_changed=on_prompt_list_changed,
            session_id=session_id,
        )
        # sandbox-only state.
        self._service = service
        self._resources = resources
        self._org_id = org_id
        self._log_buffer = log_buffer
        self._denylist_loader = denylist_loader
        self._persistence = sandbox_persistence
        self._mcpolis_instance = mcpolis_instance
        # Sandbox-files materialise list (system-vars already
        # resolved by the manager). Threaded through to
        # ``service.session`` so the launcher writes each file into
        # the sandbox before exec.
        self._materialize_files: list[MaterializeFile] = list(
            materialize_files or [],
        )

    def _log_extras(self) -> dict[str, str]:
        return {
            "transport": "sandbox",
            "provider": self._service.name,
        }

    def _close_timeout_event_name(self) -> str:
        return "upstream.sandbox.close.timeout_cancelling"

    def _close_timeout_seconds(self) -> float:
        return CLOSE_TIMEOUT

    async def pause(self) -> SnapshotRef | None:
        """Snapshot the live sandbox via the SandboxService and
        persist the resulting ref so the next ``start()`` resumes
        from it.

        Returns the ``SnapshotRef`` that was persisted, or ``None``
        when the backend can't pause / no live session exists.
        Callers (idle reaper, admin pause-now) typically call
        :meth:`close` after a successful pause to tear down the
        local task; the persisted ref means the next session-open
        skips a cold start.
        """
        ref = await self._service.pause(self._session_id)
        if ref is None:
            return None
        await self._persist_ref(ref)
        return ref

    async def _read_resume_from(self) -> SnapshotRef | None:
        """If a previous session paused this upstream, return the
        SnapshotRef for the current provider. Mismatched-provider
        refs are deleted (the operator switched backends; the old
        snapshot belongs to a different provider that can't honor
        it)."""
        if self._persistence is None:
            return None
        try:
            persisted = await self._persistence.get(
                org_id=self._org_id, upstream_id=self._upstream.id,
            )
        except Exception:
            logger.warning(
                "sandbox.persistence.read_failed",
                upstream_id=self._upstream.id, exc_info=True,
            )
            return None
        if persisted is None:
            return None
        if persisted.paused_snapshot_id is None:
            return None
        if persisted.provider != self._service.name:
            logger.info(
                "sandbox.persistence.provider_mismatch_drop",
                upstream_id=self._upstream.id,
                stored_provider=persisted.provider,
                current_provider=self._service.name,
            )
            try:
                await self._persistence.delete(
                    org_id=self._org_id, upstream_id=self._upstream.id,
                )
            except Exception:
                logger.warning(
                    "sandbox.persistence.delete_failed",
                    upstream_id=self._upstream.id, exc_info=True,
                )
            return None
        return SnapshotRef(
            provider=persisted.provider,
            snapshot_id=persisted.paused_snapshot_id,
            metadata=dict(persisted.metadata),
        )

    async def _persist_ref(self, ref: SnapshotRef) -> None:
        from datetime import datetime, timezone

        if self._persistence is None:
            return
        try:
            await self._persistence.upsert(SandboxPersistedRef(
                provider=ref.provider,
                org_id=self._org_id,
                upstream_id=self._upstream.id,
                mcpolis_instance=self._mcpolis_instance or "default",
                sandbox_id=None,
                paused_snapshot_id=ref.snapshot_id,
                pid=None,
                metadata=dict(ref.metadata),
                cached_server_info=None,
                cached_self_description=None,
                last_updated=datetime.now(tz=timezone.utc),
            ))
        except Exception:
            logger.warning(
                "sandbox.persistence.upsert_failed",
                upstream_id=self._upstream.id, exc_info=True,
            )

    async def _run(self) -> None:
        assert self._upstream.stdio is not None

        # Wipe the per-upstream log buffer at session start so the
        # admin "Server logs" panel only shows THIS session's stderr.
        if self._log_buffer is not None:
            self._log_buffer.clear()

        # Per-session env (auth token) layered on top of static
        # upstream env by the SandboxService.
        extra_env: dict[str, str] = {}
        token = self._bearer_token or self._upstream.auth.token
        if token:
            extra_env["MCP_AUTH_TOKEN"] = token

        # The legacy egress denylist (Envoy enforcer + admin UI) was
        # deleted in Phase 4; SandboxService.session still accepts a
        # denylist for future provider support, so we pass an empty
        # tuple here. The ``denylist_loader`` parameter is preserved
        # on the constructor for forward-compat.
        denylist: list[str] = []

        # ``LogBuffer`` is a ``TextIOBase`` subclass — duck-typed to
        # satisfy the ``TextIO | None`` parameter the SandboxService
        # session() method declares. Cast to silence pyright.
        errlog = (
            cast(TextIO, self._log_buffer)
            if self._log_buffer is not None
            else None
        )

        # If a previous session paused this upstream, resume from the
        # persisted snapshot. The ref must match the current
        # provider — providers don't accept each other's snapshots.
        resume_from = await self._read_resume_from()

        try:
            async with self._service.session(
                session_id=self._session_id,
                org_id=self._org_id,
                upstream=self._upstream,
                resources=self._resources,
                denylist=denylist,
                resume_from=resume_from,
                errlog=errlog,
                extra_env=extra_env,
                materialize_files=self._materialize_files,
            ) as sandbox_session:
                # Expose the backend's fatal-transport signal so the
                # manager can reconnect a dead shared session instead
                # of reusing the zombie (see ``is_transport_alive``).
                self._transport_failed = sandbox_session.transport_failed
                # NOTE (R5): deliberately NO ``read_timeout_seconds`` here.
                # The dispatch stall-recovery path (ToolRouter) bounds slow
                # / silently-stalled requests with ``asyncio.wait_for`` +
                # a ping liveness probe, whose timeout raises
                # ``asyncio.TimeoutError`` — which ``is_transport_stall``
                # classifies as a stall, so the gateway heals + retries.
                # Setting ``read_timeout_seconds`` on the ClientSession
                # instead would raise ``McpError(408 REQUEST_TIMEOUT)``,
                # which ``is_transport_stall`` does NOT classify (only
                # negative/positive 32600 "Session terminated" and
                # CONNECTION_CLOSED) — silently defeating recovery. Pick
                # one path; this is the wait_for path. See
                # tool_registry.is_transport_stall and tool_router.
                session = ClientSession(
                    sandbox_session.read_stream,
                    sandbox_session.write_stream,
                    message_handler=build_tool_change_message_handler(
                        self._upstream.id,
                        self._on_tool_list_changed,
                        self._on_resource_list_changed,
                        self._on_prompt_list_changed,
                    ),
                )
                async with session:
                    try:
                        init_result = await init_with_exit_race(
                            session, sandbox_session.exit_signal,
                        )
                    except Exception as exc:
                        self._session_future.set_exception(exc)
                        return

                    si = init_result.serverInfo
                    self.server_info = ServerInfo(
                        name=si.name, version=si.version, title=si.title,
                    )
                    raw_ext: object = getattr(
                        init_result.capabilities, "extensions", None,
                    )
                    capabilities_extensions: dict[str, dict[str, Any]] = {}
                    if isinstance(raw_ext, dict):
                        for ext_key, ext_val in raw_ext.items():  # type: ignore[reportUnknownVariableType]
                            if isinstance(ext_key, str) and isinstance(ext_val, dict):
                                capabilities_extensions[ext_key] = dict(ext_val)  # type: ignore[reportUnknownArgumentType]
                    self.self_description = UpstreamSelfDescription(
                        name=si.name,
                        version=si.version,
                        instructions=init_result.instructions,
                        description=getattr(si, "description", None),
                        website_url=si.websiteUrl,
                        capabilities_extensions=capabilities_extensions,
                    )
                    logger.info(
                        "upstream.sandbox.connection.established",
                        upstream_id=self._upstream.id,
                        provider=self._service.name,
                    )
                    self._session_future.set_result(session)

                    await self._shutdown_event.wait()
        except Exception as exc:
            if not self._session_future.done():
                self._session_future.set_exception(exc)
