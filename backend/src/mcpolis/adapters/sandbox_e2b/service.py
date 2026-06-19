"""``E2BSandboxService`` — hosted sandbox provider.

Operates entirely against the abstract :class:`E2BClient` Protocol,
so the service is fully testable against a mock without the
``e2b`` SDK installed. The real-SDK adapter at
:mod:`mcpolis.adapters.sandbox_e2b.real_client` plugs in via
constructor injection.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TextIO

import anyio
import structlog
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from mcp import types
from mcp.shared.message import SessionMessage

from mcpolis.adapters.sandbox_e2b.client import (
    E2BAuthError,
    E2BClient,
    E2BNotFoundError,
    E2BProcessHandle,
    E2BQuotaError,
    E2BSandboxHandle,
    E2BSDKError,
)
from mcpolis.adapters.sandbox_e2b.template_grid import (
    E2BTemplateGrid,
    language_for_command,
)
from datetime import datetime, timezone

from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.services.sandbox_path import confine_to_sandbox_home
from mcpolis.domain.services.stdout_framing import BoundedLineBuffer
from mcpolis.domain.services.system_variables import DEFAULT_SANDBOX_HOME
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_services.exit_signal import ExitSignalImpl
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

# Metadata key used to round-trip the E2B volume id through
# ``SandboxPersistedRef.metadata`` (a free-form ``dict[str, str]``
# the persistence layer treats as opaque). Documented as a constant
# so reconciler / teardown / mount paths all reference the same
# string and can't drift.
VOLUME_METADATA_KEY: str = "e2b_volume_id"

# Mount path inside the sandbox where a persistent volume gets
# attached. Exposed as ``/data`` to match the own-runner's loopback
# image convention so MCPs that wrote to ``/data`` against the old
# backend continue to find their state in the same place. Hard-coded
# rather than configurable because there's no concrete user need for
# multi-mount and the simpler API is easier to reason about.
PERSISTENT_VOLUME_MOUNT_PATH: str = "/data"

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Opt-in raw-stdout tracing for diagnosing the intermittent
# post-reattach discovery stall (some JSON-RPC responses stop arriving
# after a ``connect_command`` reattach). When ``MCPOLIS_E2B_DEBUG_RAW_STDOUT=1``
# the stdout demux logs, per inbound JSON-RPC line, a ``stdout.line``
# event the instant envd delivers it AND a ``stdout.forwarded`` event
# once the ClientSession read loop has consumed it. A ``line`` without
# a matching ``forwarded`` ⇒ our read loop is blocked (head-of-line);
# a missing ``line`` for a pending request id ⇒ envd never delivered
# the bytes. Off in prod; near-zero overhead when unset.
_E2B_DEBUG_RAW_STDOUT: bool = os.environ.get("MCPOLIS_E2B_DEBUG_RAW_STDOUT") == "1"

# Surface npm/uv install progress to the operator's "Server logs"
# pane. Without these, ``npx -y`` / ``uvx`` are nearly silent in
# the non-TTY sandbox — operators see a blank log during a 30s+
# cold install and assume the registration is hung. Mirrors the
# own-runner defaults set in
# ``runner/internal/runtime/podman/podman.go`` so the two backends
# behave the same. Upstream-supplied ``cfg.env`` overrides these
# (the merged-env dict is built defaults → cfg.env → extra_env).
_NPM_UV_LOG_DEFAULTS: dict[str, str] = {
    "NPM_CONFIG_LOGLEVEL": "info",
    "NPM_CONFIG_FUND": "false",
    "NPM_CONFIG_AUDIT": "false",
    "NPM_CONFIG_PROGRESS": "false",
    "UV_NO_PROGRESS": "1",
}

# Docker daemon readiness probe (docker-language sandboxes). A freshly
# launched ``dockerd`` can answer a single ``docker info`` and then be
# briefly unreachable for the immediately-following ``docker run`` — the
# race that made docker-MCP startup flaky. We require several CONSECUTIVE
# successful probes so "ready" means "stably serving", not "answered once".
_DOCKER_POLL_INTERVAL = 0.5
_DOCKER_MAX_POLLS = 120  # 120 × 0.5 s = 60 s budget
_DOCKER_READY_CONSECUTIVE = 3
# Budget for adopting a boot-managed daemon (systemd's docker.service,
# which the template image enables): shorter than the manual-launch
# budget — systemd brings dockerd up within seconds or it's wedged, and
# the manual fallback below still gets the full budget afterwards.
_DOCKER_ADOPT_MAX_POLLS = 60  # 60 × 0.5 s = 30 s budget


class E2BSandboxService:
    """SandboxService backed by E2B."""

    name: SandboxProviderName = "e2b"

    def __init__(
        self,
        client: E2BClient,
        *,
        mcpolis_instance: str,
        template_grid: E2BTemplateGrid | None = None,
        on_timeout_seconds: int,
        persistence: SandboxPersistenceRepository | None = None,
        volumes_enabled: bool = False,
        reuse_sandboxes_on_restart: bool = False,
    ) -> None:
        self._client = client
        self._mcpolis_instance = mcpolis_instance
        self._grid = template_grid or E2BTemplateGrid()
        self._on_timeout_seconds = on_timeout_seconds
        # Persistence handles two pieces of provider-side state per
        # ``(org, upstream)``:
        #   - ``paused_snapshot_id`` (snapshot resume; existing).
        #   - ``metadata[VOLUME_METADATA_KEY]`` (E2B volume id,
        #     mounted at PERSISTENT_VOLUME_MOUNT_PATH when the
        #     upstream opts in via ``stdio.persistent_disk_enabled``).
        # ``None`` ⇔ persistent-disk feature is disabled for this
        # service instance (tests, dev). _open_sandbox treats a
        # missing repo as "no volume mount" regardless of the
        # upstream's persistent_disk_enabled flag.
        self._persistence = persistence
        # Operator switch for the Volumes API. The E2B account must
        # have Volumes enabled in the dashboard (otherwise the SDK
        # returns ``403: use of volumes is not enabled``); this
        # backend-side boolean lets the operator delay opting in to
        # the feature until they've flipped that account-level
        # switch. ``False`` ⇔ ``capabilities().supports_persistent_disk``
        # stays ``False`` and ``_open_sandbox`` skips volume mounts
        # even when the upstream's stdio config opts in.
        self._volumes_enabled = volumes_enabled
        # session_id → live sandbox handle. Populated on session()
        # entry, cleared on session() exit. ``pause(session_id)``
        # looks the handle up here and calls handle.pause(). The dict
        # is process-local; multi-instance safety is handled at the
        # SandboxPersistenceRepository layer (step 10).
        self._live_sandboxes: dict[str, E2BSandboxHandle] = {}
        # Set of session ids that called ``pause()`` during their
        # session. The session() finally block uses this to skip the
        # default ``sandbox.kill()`` cleanup — once paused, the
        # sandbox is reachable later via ``Sandbox.connect``, but
        # ``kill()`` would destroy the snapshot and break resume.
        self._paused_sessions: set[str] = set()
        # Reuse-on-restart behaviour. When ``True``, ``_session_cm``
        # looks up ``persistence`` for a live ref before creating a
        # fresh sandbox so the next boot can reattach across deploys.
        # Requires ``persistence`` to also be set; without it, there's
        # no way to round-trip the ``(sandbox_id, pid)`` tuple across
        # boots. Default ``False`` to keep tests' kill-on-exit
        # contract; production wires this to ``True`` from
        # ``MCPOLIS_E2B_REUSE_SANDBOXES_ON_RESTART``.
        self._reuse_sandboxes_on_restart = (
            reuse_sandboxes_on_restart and persistence is not None
        )
        # session_id → True for sessions whose teardown should skip
        # ``sandbox.kill()`` so the sandbox survives the process exit
        # for the next boot's reconnect. The lifespan handler marks
        # every active session before tearing down on graceful
        # shutdown (SIGTERM, deploy). Default behaviour for any other
        # teardown path (user Stop, user Delete, idle disconnect) is
        # to kill — paused sandboxes accrue per-GB-hour storage cost
        # so we don't want them lingering when the user has signaled
        # they're done. Populated only via
        # :meth:`mark_session_preserve_on_close` (and its bulk peer
        # :meth:`mark_all_active_sessions_preserve_on_close`); cleared
        # in the ``_session_cm`` finally block as the session exits.
        self._preserve_on_close: dict[str, bool] = {}
        # session_id → (org_id, upstream_id) for sessions that wrote a
        # persistence ref via ``_persist_live_ref``. Lets the finally
        # block delete the now-stale ref when killing the sandbox so
        # the next boot reconnect doesn't waste an E2B API call
        # against a dead ID. Populated in ``_session_cm`` after the
        # ref upsert; cleared in finally alongside the sandbox
        # registration.
        self._session_owners: dict[str, tuple[str, str]] = {}

    # ---------- capabilities + validation ----------

    def capabilities(self) -> SandboxCapabilities:
        # E2B ships paired CPU/RAM templates — only the published
        # tuples in ``CPU_RAM_PAIRS`` are valid. Disk is fixed at
        # template build time (no per-session control), so each combo
        # carries ``disk_gb=0`` for wire-shape parity with the other
        # backends.
        combos = tuple(
            SandboxResourceCombo(
                cpu_vcpus=float(cpu), memory_mb=ram, disk_gb=0,
            )
            for (cpu, ram) in self._grid.cpu_ram_pairs
        )
        return SandboxCapabilities(
            provider="e2b",
            allowed_cpu_vcpus=self._grid.allowed_cpu_vcpus,
            allowed_memory_mb=self._grid.allowed_memory_mb,
            # E2B fixes storage at template build time — the disk
            # axis is hidden from the admin UI's combined-picker
            # label when this backend is active. See plan
            # §"Per-MCP resource configuration" capability table.
            allowed_disk_gb=(),
            allowed_combinations=combos,
            supports_pause_resume=True,
            supports_egress_filtering=False,
            # Persistent storage is provided via E2B Volumes mounted
            # at PERSISTENT_VOLUME_MOUNT_PATH. True only when BOTH
            # a persistence repository was injected (so the volume
            # id can round-trip across sessions) AND the operator
            # has set ``MCPOLIS_E2B_VOLUMES_ENABLED=true`` (so the
            # E2B account is known to have Volumes enabled — the
            # SDK 403s otherwise).
            supports_persistent_disk=(
                self._persistence is not None and self._volumes_enabled
            ),
        )

    def validate_resources(self, resources: SandboxResources) -> None:
        # CPU + RAM must each be in the published allowed sets, AND
        # the (cpu, ram) pairing must exist in the template grid.
        if resources.cpu_vcpus not in self._grid.allowed_cpu_vcpus:
            raise ResourcesUnsupported(
                "cpu_vcpus", resources.cpu_vcpus,
                allowed=self._grid.allowed_cpu_vcpus,
            )
        if resources.memory_mb not in self._grid.allowed_memory_mb:
            raise ResourcesUnsupported(
                "memory_mb", resources.memory_mb,
                allowed=self._grid.allowed_memory_mb,
            )
        if not self._grid.is_valid_pairing(
            cpu_vcpus=resources.cpu_vcpus, memory_mb=resources.memory_mb,
        ):
            raise ResourcesUnsupported(
                "cpu_vcpus",
                (resources.cpu_vcpus, resources.memory_mb),
                allowed=self._grid.cpu_ram_pairs,
            )
        # E2B doesn't expose user-configurable disk — surface a
        # specific error if the upstream tries.
        if resources.disk_gb != 0:
            raise ResourcesUnsupported(
                "disk_gb", resources.disk_gb, allowed=(0,),
            )

    def sandbox_home(self, *, session_id: str) -> str:
        # Every published mcpolis template inherits the stock E2B SDK
        # user (``user`` with ``HOME=/home/user``); the container home
        # is fixed regardless of session, so ``session_id`` is ignored.
        # Guarded by ``test_e2b_template_home_consistency.py``.
        _ = session_id
        return DEFAULT_SANDBOX_HOME

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
        # E2B has no first-party egress filtering — denylist is
        # documented unsupported (plan §Tradeoffs). Accept the value
        # so the SandboxService boundary stays uniform; just don't
        # apply it here.
        _ = denylist
        return self._session_cm(
            session_id=session_id,
            org_id=org_id,
            upstream=upstream,
            resources=resources,
            resume_from=resume_from,
            errlog=errlog,
            extra_env=extra_env,
            materialize_files=materialize_files,
        )

    @asynccontextmanager
    async def _session_cm(
        self,
        *,
        session_id: str,
        org_id: str,
        upstream: UpstreamDefinition,
        resources: SandboxResources,
        resume_from: SnapshotRef | None,
        errlog: TextIO | None,
        extra_env: dict[str, str] | None,
        materialize_files: Sequence[MaterializeFile] | None = None,
    ) -> AsyncIterator[SandboxSession]:
        cfg = upstream.stdio
        if cfg is None:
            raise ValueError(
                f"upstream {upstream.id!r} has transport=stdio but"
                " stdio config is missing",
            )
        # Defaults first, upstream cfg.env next, extra_env last —
        # later writes win, mirroring the own-runner ``--env=`` order.
        merged_env: dict[str, str] = dict(_NPM_UV_LOG_DEFAULTS)
        merged_env.update(cfg.env)
        if extra_env:
            merged_env.update(extra_env)

        # Anyio memory streams shaped exactly like
        # ``mcp.client.stdio.stdio_client``'s output. ``read_stream``
        # carries SessionMessage|Exception (parser failures surface as
        # exceptions on the stream); ``write_stream`` accepts
        # SessionMessages from ClientSession.
        read_writer, read_stream = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        write_stream, write_reader = anyio.create_memory_object_stream[
            SessionMessage
        ](0)

        # Fatal-transport signal. Set when the stdin pump gives up on
        # an unrecoverable failure (sandbox gone, reattach/send failed)
        # — NOT on a transient auto-pause the pump reattaches through.
        # The connection task surfaces this via ``is_transport_alive()``
        # so the manager reconnects a dead shared session instead of
        # reusing the zombie. See ``_fail_transport`` below.
        transport_failed = asyncio.Event()

        async def _fail_transport() -> None:
            """Mark the transport dead and close the read side so the
            ``ClientSession`` read loop ends and fails every in-flight
            request with ``CONNECTION_CLOSED`` immediately — rather than
            each waiting out its full ``wait_for`` timeout. (Sending an
            ``Exception`` object down ``read_writer`` does NOT do this:
            MCP SDK ≥1.x routes it to the message handler, which drops
            it, leaving the pending request to hang.)
            """
            transport_failed.set()
            try:
                await read_writer.aclose()
            except Exception:
                pass

        # Stdout demux: re-assemble newline-framed JSON-RPC into
        # SessionMessages. The bounded buffer caps the in-progress
        # leftover so a newline-free stream can't grow per-session memory
        # without bound (SBX-7 / BUG-6).
        stdout_lines = BoundedLineBuffer()

        async def on_stdout(chunk: bytes) -> None:
            text = chunk.decode("utf-8", errors="replace")
            for line in stdout_lines.feed(text):
                if not line:
                    continue
                try:
                    msg = types.JSONRPCMessage.model_validate_json(line)
                except Exception as exc:
                    # Non-JSON-RPC lines on stdout are typically
                    # install/startup chatter (npm/uvx print there
                    # under some configurations). Surface them to the
                    # operator's stderr log so the "Server logs" pane
                    # isn't silent during cold installs, while still
                    # informing ClientSession the line was bad.
                    logger.warning(
                        "sandbox.e2b.stdout.parse_failed",
                        error=str(exc), line_preview=line[:120],
                    )
                    if errlog is not None:
                        try:
                            errlog.write(line + "\n")
                        except Exception:
                            logger.warning(
                                "sandbox.e2b.stdout.errlog_write_failed",
                                exc_info=True,
                            )
                    await read_writer.send(exc)
                    continue
                if _E2B_DEBUG_RAW_STDOUT:
                    _root = msg.root
                    logger.info(
                        "sandbox.e2b.stdout.line",
                        session_id=session_id,
                        msg_id=getattr(_root, "id", None),
                        method=getattr(_root, "method", None),
                        nbytes=len(line),
                    )
                await read_writer.send(SessionMessage(message=msg))
                if _E2B_DEBUG_RAW_STDOUT:
                    logger.info(
                        "sandbox.e2b.stdout.forwarded",
                        session_id=session_id,
                        msg_id=getattr(msg.root, "id", None),
                    )

        # Per-session exit signal — fed by the watch_stream task below
        # (exit code) and the on_stderr callback (stderr tail). The
        # connection task races ``exit_signal.wait()`` against
        # ``session.initialize()`` to fail-fast on bogus commands
        # without paying the full INIT_TIMEOUT.
        exit_signal = ExitSignalImpl()

        async def on_stderr(chunk: bytes) -> None:
            # Tee into the exit-signal tail FIRST so a stderr-then-die
            # race still has the bytes available when the connection
            # task reads ``snapshot()`` — even if the errlog write
            # below raises and short-circuits the rest of this callback.
            exit_signal.append_stderr(chunk)
            if errlog is None:
                return
            try:
                errlog.write(chunk.decode("utf-8", errors="replace"))
            except Exception:
                logger.warning(
                    "sandbox.e2b.stderr.write_failed", exc_info=True,
                )

        argv = [cfg.command, *list(cfg.args)]
        # Acquire the (sandbox, process) pair: try reconnect to a
        # persisted live ref first if reuse-on-restart is enabled,
        # else fresh-create. Returns ``(sandbox, process,
        # was_reconnect)``; ``was_reconnect=True`` skips persisting
        # a new ref since the existing ref is still valid.
        sandbox, process, _was_reconnect = await self._acquire_session_handles(
            session_id=session_id,
            org_id=org_id,
            upstream=upstream,
            resources=resources,
            resume_from=resume_from,
            extra_env=extra_env,
            argv=argv,
            merged_env=merged_env,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            materialize_files=materialize_files,
        )
        # Register the live handle so ``pause(session_id)`` can find
        # it. Cleared in the ``finally`` below regardless of how the
        # session exits, so a subsequent pause() for the same id
        # cleanly returns None.
        self._live_sandboxes[session_id] = sandbox

        # Persist the live ref iff reuse-on-restart is enabled and
        # this isn't an explicit-pause/resume flow (resume_from gets
        # its own persistence path on pause()). When ``was_reconnect``
        # is true, the existing ref is still valid for the same
        # ``(sandbox_id, pid)``; we just bump ``last_updated`` so an
        # operator can see "this ref is being actively used".
        if (
            self._reuse_sandboxes_on_restart
            and resume_from is None
            and self._persistence is not None
        ):
            try:
                await self._persist_live_ref(
                    org_id=org_id,
                    upstream=upstream,
                    sandbox_id=sandbox.sandbox_id,
                    pid=process.pid,
                )
                self._session_owners[session_id] = (org_id, upstream.id)
            except Exception:
                # Persistence failure is non-fatal: the session
                # still works, but on next boot we'll fall back to
                # fresh-create instead of reconnect. Log so this
                # surfaces in operator dashboards.
                logger.warning(
                    "sandbox.e2b.persist_live_ref.failed",
                    session_id=session_id, exc_info=True,
                )

        # E2B's ``on_timeout=pause`` lifecycle severs the streaming
        # RPC that delivers stdout/stderr from a long-lived
        # ``run_command`` when the sandbox auto-pauses; ``auto_resume``
        # re-establishes unary calls (``send_stdin``) on the next API
        # hit but does NOT reconnect the streaming RPC. Without
        # detection, the next tool call sends stdin successfully, the
        # MCP process replies, and the response goes nowhere — the
        # tool call hangs indefinitely.
        #
        # Detect the dead stream by watching ``process.wait()`` in a
        # sidecar task: any return (clean exit OR raised exception)
        # means "no more output will arrive on this handle." On hit,
        # the stdin pump reattaches a fresh handle pointed at the same
        # pid via ``sandbox.connect_command(pid=…)``. The pid is
        # stable across pause/resume in E2B v2.
        stream_dead = asyncio.Event()

        async def watch_stream(p: E2BProcessHandle) -> None:
            exit_code: int | None = None
            try:
                exit_code = await p.wait()
            except BaseException:
                # Any exception out of wait() also means the events
                # stream is no longer delivering — fold it into the
                # same dead-stream signal so the pump reattaches.
                # ``exit_code`` stays ``None``; the snapshot will
                # report "exited (code unknown)".
                pass
            finally:
                # ``mark_exited`` is no-op after the first call, so
                # the post-reattach watcher (re-spawned at line ~570
                # when the SDK's auto-pause severs the streaming RPC)
                # can't overwrite a real subprocess-exit observation
                # captured during the init window.
                exit_signal.mark_exited(exit_code)
                stream_dead.set()

        watch_task: asyncio.Task[None] = asyncio.create_task(
            watch_stream(process),
        )

        # Stdin pump: serialise outgoing SessionMessages to JSON-RPC
        # lines and feed them into the sandbox process's stdin via the
        # SDK's send_stdin. On any send failure (sandbox killed
        # underneath us, network blip, SDK error), surface the
        # exception via the read stream so ClientSession raises on
        # the in-flight call_tool instead of waiting forever for a
        # stdout response that will never come — silent return here
        # is what caused tool calls to hang after the E2B sandbox
        # had been killed by the on_timeout backstop.
        async def stdin_pump() -> None:
            nonlocal process, watch_task
            try:
                async with write_reader:
                    async for session_message in write_reader:
                        if stream_dead.is_set():
                            # Time ``connect_command`` — this call
                            # encompasses E2B's auto_resume of the
                            # paused sandbox + re-establishing the
                            # streaming RPC. Surfaced as
                            # ``reattach_duration_ms`` on the
                            # ``sandbox.e2b.reattach.ok`` event so
                            # ops dashboards can graph wake-from-
                            # paused latency over time and alert on
                            # regressions (E2B side or ours). The
                            # integration suite extracts this same
                            # field into a ``wake_from_paused_ms``
                            # column.
                            connect_start = time.monotonic()
                            try:
                                new_handle = await sandbox.connect_command(
                                    pid=process.pid,
                                    on_stdout=on_stdout,
                                    on_stderr=on_stderr,
                                )
                            except (
                                ConnectionError, OSError, E2BSDKError,
                            ) as exc:
                                logger.warning(
                                    "sandbox.e2b.reattach.failed",
                                    session_id=session_id,
                                    pid=process.pid,
                                    error=str(exc),
                                    reattach_duration_ms=round(
                                        (time.monotonic() - connect_start) * 1000, 1,
                                    ),
                                )
                                await _fail_transport()
                                return
                            reattach_duration_ms = round(
                                (time.monotonic() - connect_start) * 1000, 1,
                            )
                            # When ``commands.connect(pid)``
                            # triggers ``auto_resume``, E2B replaces
                            # our configured ``on_timeout`` with the
                            # SDK's default (``default_sandbox_timeout``
                            # = 300s). Re-apply our value so the
                            # ``MCPOLIS_E2B_IDLE_PAUSE_SECONDS`` knob
                            # actually holds across multiple
                            # auto-pause/reattach cycles. Verified
                            # 2026-05-01 via the diagnostic at
                            # ``backend/tests/integration/diagnose_double_reattach.py``.
                            set_timeout_start = time.monotonic()
                            set_timeout_duration_ms: float | None = None
                            try:
                                await sandbox.set_timeout(
                                    self._on_timeout_seconds,
                                )
                                set_timeout_duration_ms = round(
                                    (time.monotonic() - set_timeout_start) * 1000, 1,
                                )
                            except (
                                ConnectionError, OSError, E2BSDKError,
                            ) as exc:
                                # Best-effort: a failed re-apply only
                                # costs extra running time, doesn't
                                # break the session. Log and continue.
                                logger.warning(
                                    "sandbox.e2b.reattach.set_timeout_failed",
                                    session_id=session_id,
                                    error=str(exc),
                                )
                            logger.info(
                                "sandbox.e2b.reattach.ok",
                                session_id=session_id,
                                pid=new_handle.pid,
                                reattach_duration_ms=reattach_duration_ms,
                                set_timeout_duration_ms=set_timeout_duration_ms,
                            )
                            # Tear down the OLD handle's streaming
                            # RPC explicitly before dropping the
                            # reference. Without this, the SDK's
                            # events-iteration generator (holding an
                            # open httpx stream context manager) is
                            # left to be aclose'd by GC at an
                            # arbitrary later point, racing the
                            # httpx generator's own ``__aexit__``
                            # and surfacing as ``RuntimeError:
                            # athrow(): asynchronous generator is
                            # already running`` on stderr after the
                            # last scenario finishes. Best-effort:
                            # release errors are noisy in logs but
                            # don't break the session.
                            old_process = process
                            try:
                                await old_process.release()
                            except (
                                ConnectionError, OSError, E2BSDKError,
                            ) as exc:
                                logger.warning(
                                    "sandbox.e2b.reattach.release_failed",
                                    session_id=session_id,
                                    error=str(exc),
                                )
                            process = new_handle
                            stream_dead.clear()
                            watch_task = asyncio.create_task(
                                watch_stream(new_handle),
                            )
                        body = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True,
                        )
                        try:
                            await process.send_stdin(
                                (body + "\n").encode("utf-8"),
                            )
                        except (ConnectionError, OSError, E2BSDKError) as exc:
                            logger.warning(
                                "sandbox.e2b.stdin.send_failed",
                                session_id=session_id,
                                error=str(exc),
                            )
                            await _fail_transport()
                            return
            except anyio.ClosedResourceError:
                return

        stdin_task = asyncio.create_task(stdin_pump())

        try:
            yield SandboxSession(
                read_stream=read_stream,
                write_stream=write_stream,
                exit_signal=exit_signal,
                transport_failed=transport_failed,
            )
        finally:
            # Drop the live-handle registration before any cleanup so
            # pause(session_id) called concurrently with teardown
            # safely returns None instead of racing into a
            # half-killed sandbox.
            self._live_sandboxes.pop(session_id, None)
            was_paused = session_id in self._paused_sessions
            self._paused_sessions.discard(session_id)
            preserve = self._preserve_on_close.pop(session_id, False)
            owner = self._session_owners.pop(session_id, None)

            await write_stream.aclose()
            try:
                await asyncio.wait_for(stdin_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stdin_task.cancel()

            # Cancel the stream-death watcher. Normal teardown for a
            # healthy session: the watcher is still pending (process
            # still alive); cancel it. Post-pause teardown: the
            # watcher already finished and set ``stream_dead``;
            # cancel is a no-op.
            watch_task.cancel()
            try:
                await watch_task
            except (asyncio.CancelledError, Exception):
                pass

            # Skip the destructive cleanup (process.kill +
            # sandbox.kill) in two cases:
            #
            # 1. ``was_paused`` — the session called ``pause()``
            #    explicitly. The snapshot is the new state; killing
            #    would destroy it.
            # 2. ``preserve`` — the lifespan handler marked this
            #    session preserve-on-close (graceful shutdown:
            #    SIGTERM, deploy). The sandbox must outlive this
            #    process so the next boot's ``_try_reconnect`` picks
            #    it up. E2B's ``on_timeout=pause`` auto-pauses it
            #    after the configured idle window so we're not
            #    holding a live VM indefinitely.
            #
            # Default behaviour (any non-shutdown teardown — user
            # Stop, user Delete, idle disconnect, error) is to kill.
            # Paused sandboxes accrue per-GB-hour storage cost so we
            # don't leave them lingering once the user signals
            # they're done.
            #
            # Always release the streaming RPC even when skipping
            # kill — the underlying httpx generator needs a
            # controlled close (see the post-reattach release
            # comment block in the stdin pump for the
            # GC-vs-__aexit__ race rationale).
            try:
                await process.release()
            except E2BSDKError:
                logger.warning(
                    "sandbox.e2b.process.release_failed", exc_info=True,
                )
            should_kill = not was_paused and not preserve
            if should_kill:
                # Drop the now-stale persistence ref BEFORE the kill
                # so a crash mid-teardown can't leave a ref pointing
                # at a dead sandbox id (which the next boot reconnect
                # would waste an E2B API call on before falling
                # through to fresh-create).
                if owner is not None and self._persistence is not None:
                    owner_org_id, owner_upstream_id = owner
                    try:
                        await self._persistence.delete(
                            org_id=owner_org_id,
                            upstream_id=owner_upstream_id,
                        )
                    except Exception:
                        logger.warning(
                            "sandbox.e2b.persistence.delete_on_kill_failed",
                            org_id=owner_org_id,
                            upstream_id=owner_upstream_id,
                            exc_info=True,
                        )
                try:
                    await process.kill()
                except E2BSDKError:
                    logger.warning(
                        "sandbox.e2b.process.kill_failed", exc_info=True,
                    )
                try:
                    await sandbox.kill()
                except E2BSDKError:
                    logger.warning(
                        "sandbox.e2b.sandbox.kill_failed", exc_info=True,
                    )
            await read_stream.aclose()

    async def _acquire_session_handles(
        self,
        *,
        session_id: str,
        org_id: str,
        upstream: UpstreamDefinition,
        resources: SandboxResources,
        resume_from: SnapshotRef | None,
        extra_env: dict[str, str] | None,
        argv: list[str],
        merged_env: dict[str, str],
        on_stdout: "Callable[[bytes], Awaitable[None] | None]",
        on_stderr: "Callable[[bytes], Awaitable[None] | None]",
        materialize_files: Sequence[MaterializeFile] | None = None,
    ) -> tuple[E2BSandboxHandle, E2BProcessHandle, bool]:
        """Return ``(sandbox, process, was_reconnect)`` for a session.

        Three paths:

        1. **Resume from snapshot** (caller passed ``resume_from``):
           ``connect_sandbox(snapshot_id)`` + fresh ``run_command``.
           This is the explicit-pause path — the previous session
           was paused, the snapshot is the resumed-from id. Always
           returns ``was_reconnect=False`` because the MCP process
           inside the snapshot is dropped and a new one starts.
        2. **Reuse-on-restart** (``_reuse_sandboxes_on_restart`` is
           on AND persistence has a live ref):
           ``connect_sandbox(sandbox_id)`` + skip ``run_command``,
           instead ``connect_command(pid)`` to reattach the
           streaming RPC against the existing MCP process. Returns
           ``was_reconnect=True`` so the caller skips the
           persist-ref step (existing ref is still valid). Falls
           through to (3) on any failure (sandbox died, network
           blip). Reattach is unconditional: a config edit on disk
           does NOT propagate until the user explicitly Stop+Restart
           the upstream.
        3. **Fresh create** (default): ``create_sandbox`` +
           ``run_command``. Returns ``was_reconnect=False``.

        Path (2) is what eliminates deploy downtime — the
        ~25 s cold-install becomes a ~700 ms wake-from-paused
        because the MCP process and its filesystem survive in the
        sandbox snapshot from the previous boot.
        """
        # Path 1: explicit-pause resume. Existing flow, unchanged.
        if resume_from is not None:
            sandbox = await self._open_sandbox(
                org_id=org_id, upstream=upstream, resources=resources,
                resume_from=resume_from,
            )
            await self._materialize_files(
                sandbox=sandbox,
                upstream_id=upstream.id,
                materialize_files=materialize_files,
            )
            process = await sandbox.run_command(
                argv, env=merged_env,
                on_stdout=on_stdout, on_stderr=on_stderr,
            )
            return sandbox, process, False

        # Path 2: reuse-on-restart — try reconnect to a persisted
        # live ref. Only fires when the operator has opted in AND
        # persistence is wired AND a recoverable ref exists.
        if (
            self._reuse_sandboxes_on_restart
            and self._persistence is not None
        ):
            reconnect = await self._try_reconnect(
                org_id=org_id,
                upstream=upstream,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            if reconnect is not None:
                logger.info(
                    "sandbox.e2b.reconnect.ok",
                    session_id=session_id,
                    org_id=org_id,
                    upstream_id=upstream.id,
                    sandbox_id=reconnect[0].sandbox_id,
                    pid=reconnect[1].pid,
                )
                return reconnect[0], reconnect[1], True

        # Path 3: fresh create. The default flow.
        #
        # Persist a "creating" marker BEFORE create_sandbox makes the
        # sandbox provider-visible, so a reconcile racing into the
        # create/persist window recognizes the sandbox as actively-managed
        # and leaves it alone (SBX-CONC-4). The marker only matters when
        # persistence + reuse is wired (the reconciler runs only then);
        # ``session()`` overwrites it with the real ref via
        # ``_persist_live_ref`` once create succeeds. On any failure here
        # we restore the prior ref / drop the marker so a later reconcile
        # can reap the leaked sandbox.
        write_marker = (
            self._reuse_sandboxes_on_restart and self._persistence is not None
        )
        prior_ref: SandboxPersistedRef | None = None
        if write_marker:
            prior_ref = await self._persist_creating_marker(
                org_id=org_id, upstream=upstream,
            )
        try:
            sandbox = await self._open_sandbox(
                org_id=org_id, upstream=upstream, resources=resources,
                resume_from=None,
            )
            await self._materialize_files(
                sandbox=sandbox,
                upstream_id=upstream.id,
                materialize_files=materialize_files,
            )
            # docker-language sandboxes have the Docker engine installed
            # but no running daemon — start it now before the MCP command
            # runs. Resume (path 1) and reconnect (path 2) skip this:
            # resume restores the frozen microVM state (dockerd survives
            # the snapshot), and reconnect reattaches to an already-live
            # sandbox.
            cfg = upstream.stdio
            if cfg is not None and language_for_command(cfg.command) == "docker":
                await self._start_docker_daemon(sandbox)
            process = await sandbox.run_command(
                argv, env=merged_env,
                on_stdout=on_stdout, on_stderr=on_stderr,
            )
        except BaseException:
            if write_marker:
                await self._restore_or_clear_creating_marker(
                    org_id=org_id, upstream=upstream, prior=prior_ref,
                )
            raise
        return sandbox, process, False

    async def _try_reconnect(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        on_stdout: "Callable[[bytes], Awaitable[None] | None]",
        on_stderr: "Callable[[bytes], Awaitable[None] | None]",
    ) -> tuple[E2BSandboxHandle, E2BProcessHandle] | None:
        """Look up persistence for a live ref and try to reconnect.

        Returns ``(sandbox, process)`` on success, ``None`` on any
        miss/failure (caller falls back to fresh create). Logs the
        specific reason for the miss so operators can diagnose
        unexpected fresh-creates.
        """
        assert self._persistence is not None  # guarded by caller
        try:
            ref = await self._persistence.get(
                org_id=org_id, upstream_id=upstream.id,
            )
        except Exception:
            logger.warning(
                "sandbox.e2b.reconnect.persistence_read_failed",
                org_id=org_id, upstream_id=upstream.id, exc_info=True,
            )
            return None
        if ref is None or ref.provider != "e2b":
            return None
        if ref.sandbox_id is None or ref.pid is None:
            # Either pre-feature ref (no pid) or paused-only
            # snapshot ref. Neither is reconnectable; force fresh.
            # Logged so a deploy that fresh-creates everything has a
            # paper trail (silent fall-through is hard to diagnose).
            logger.info(
                "sandbox.e2b.reconnect.unreusable_ref",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, pid=ref.pid,
                paused_snapshot_id=ref.paused_snapshot_id,
            )
            return None
        # Drift contract: a config edit on disk does NOT take effect
        # until the user explicitly Stop+Restart the upstream. On
        # boot reconnect we therefore reattach unconditionally to
        # whatever sandbox is alive, regardless of whether the live
        # config has changed since the sandbox was created. The
        # previous config-hash gate silently applied pending edits
        # across deploys, which surprised operators who hadn't asked
        # for them.
        # Try the actual reconnect.
        try:
            sandbox = await self._client.connect_sandbox(ref.sandbox_id)
        except E2BSDKError as exc:
            logger.info(
                "sandbox.e2b.reconnect.connect_sandbox_failed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, error=str(exc),
            )
            return None
        # Re-apply our configured idle timeout — connect_sandbox
        # implicitly sets the SDK default (300 s) on auto_resume,
        # discarding whatever was set at create time.
        try:
            await sandbox.set_timeout(self._on_timeout_seconds)
        except E2BSDKError:
            logger.warning(
                "sandbox.e2b.reconnect.set_timeout_failed",
                sandbox_id=ref.sandbox_id, exc_info=True,
            )
        connect_command_started = time.monotonic()
        try:
            process = await sandbox.connect_command(
                pid=ref.pid,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        except E2BSDKError as exc:
            elapsed_ms = round(
                (time.monotonic() - connect_command_started) * 1000.0, 1,
            )
            # Severity is ``warning`` (not ``info``) because this path
            # delays a user-visible Start click by up to the SDK's
            # connect_command timeout (~60s on a port-not-open
            # ``TimeoutException``) before falling back to a fresh
            # create. ``elapsed_ms`` and ``error_class`` let dashboards
            # split routine "sandbox auto-stopped" reattach failures
            # (fast, expected) from "E2B API degraded" timeouts (slow,
            # actionable) without grepping the freeform ``error``
            # string. ``fallback="fresh_create"`` makes the self-
            # healing handoff explicit so a single log line carries
            # the full story rather than implying it from the
            # ``sandbox.e2b.create`` event ~hundreds-of-ms later.
            logger.warning(
                "sandbox.e2b.reconnect.connect_command_failed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, pid=ref.pid,
                elapsed_ms=elapsed_ms,
                error_class=type(exc).__name__,
                error=str(exc),
                fallback="fresh_create",
            )
            # Process is gone but the sandbox might still be alive.
            # Kill it to avoid a leak — the caller's fresh-create
            # path will produce a new one.
            await self._kill_stale_sandbox(
                sandbox_id=ref.sandbox_id, sandbox=sandbox,
            )
            return None
        return sandbox, process

    async def _kill_stale_sandbox(
        self,
        *,
        sandbox_id: str,
        sandbox: E2BSandboxHandle | None = None,
    ) -> None:
        """Best-effort kill of a stale sandbox during reconnect recovery.

        Used by :meth:`_try_reconnect` when ``connect_command`` fails:
        the process is gone but the sandbox might still be alive, so
        we kill it to avoid a leak before the caller's fresh-create
        path produces a new one.

        Always best-effort — a failure here can't block the caller's
        fresh-create path. The reconciler is the eventual-consistency
        net for any sandbox that survives the kill attempt.
        """
        try:
            if sandbox is not None:
                await sandbox.kill()
            else:
                await self._client.kill_sandbox(sandbox_id)
        except E2BSDKError:
            logger.warning(
                "sandbox.e2b.reconnect.stale_kill_failed",
                sandbox_id=sandbox_id, exc_info=True,
            )

    async def _persist_live_ref(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        sandbox_id: str,
        pid: int,
    ) -> None:
        """Write a live-ref to persistence so the next boot can
        reconnect via :meth:`_try_reconnect`.

        Preserves any pre-existing ``metadata`` / ``paused_snapshot_id``
        on the row (e.g. ``e2b_volume_id`` for persistent-disk
        upstreams) — this is purely a write-through of the live
        identity tuple."""
        if self._persistence is None:
            return
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        merged_metadata = (
            dict(existing.metadata) if existing is not None else {}
        )
        ref = SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=upstream.id,
            mcpolis_instance=self._mcpolis_instance,
            sandbox_id=sandbox_id,
            paused_snapshot_id=(
                # Clear any stale paused-snapshot id; a live ref
                # supersedes a previous pause.
                None
            ),
            pid=pid,
            metadata=merged_metadata,
            cached_server_info=(
                existing.cached_server_info if existing is not None else None
            ),
            cached_self_description=(
                existing.cached_self_description
                if existing is not None else None
            ),
            last_updated=datetime.now(tz=timezone.utc),
        )
        await self._persistence.upsert(ref)

    async def _persist_creating_marker(
        self, *, org_id: str, upstream: UpstreamDefinition,
    ) -> SandboxPersistedRef | None:
        """Write a "creating" marker for ``(org, upstream)`` BEFORE a
        fresh ``create_sandbox`` makes the sandbox provider-visible.

        The marker is a ref with BOTH ``sandbox_id`` and
        ``paused_snapshot_id`` ``None`` — a state the normal lifecycle
        never produces (a ref always carries one or the other), so the
        reconciler can recognize it unambiguously. A boot/periodic
        reconcile that races into the create/persist window then sees an
        actively-managed ``(org, upstream)`` and skips the running
        sandbox (matched by its ``mcpolis_org`` / ``mcpolis_upstream``
        metadata) instead of killing it as an orphan.

        Returns the PRIOR ref (if any) so the caller can restore it if
        the create then fails — the marker upsert clobbers a prior
        paused-snapshot ref, and a failed create must not lose it. On a
        successful session ``_persist_live_ref`` overwrites the marker
        with the real live identity tuple.
        """
        assert self._persistence is not None
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        marker = SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=upstream.id,
            mcpolis_instance=self._mcpolis_instance,
            sandbox_id=None,
            paused_snapshot_id=None,
            pid=None,
            metadata=dict(existing.metadata) if existing is not None else {},
            cached_server_info=(
                existing.cached_server_info if existing is not None else None
            ),
            cached_self_description=(
                existing.cached_self_description
                if existing is not None else None
            ),
            last_updated=datetime.now(tz=timezone.utc),
        )
        await self._persistence.upsert(marker)
        return existing

    async def _restore_or_clear_creating_marker(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        prior: SandboxPersistedRef | None,
    ) -> None:
        """Undo a creating marker after a failed fresh create: restore
        the prior ref if there was one (so a clobbered paused-snapshot
        ref survives), else delete the marker so a later reconcile can
        reap the leaked sandbox."""
        if self._persistence is None:
            return
        try:
            if prior is not None:
                await self._persistence.upsert(prior)
            else:
                await self._persistence.delete(
                    org_id=org_id, upstream_id=upstream.id,
                )
        except Exception:
            logger.warning(
                "sandbox.e2b.creating_marker.cleanup_failed",
                org_id=org_id, upstream_id=upstream.id, exc_info=True,
            )

    async def _start_docker_daemon(self, sandbox: E2BSandboxHandle) -> None:
        """Ensure a usable dockerd inside a docker-language sandbox.

        The template image installs Docker via get.docker.com, which
        enables ``docker.service`` / ``docker.socket``, and E2B sandboxes
        boot with systemd as PID 1 — so a daemon is usually already
        starting (or serving) when the session begins. The flow:

          1. If ``docker info`` already answers, just fix the socket perms.
          2. If a boot-managed daemon is pending (dockerd process exists,
             or the systemd units are active/activating), ADOPT it: poll
             until it serves stably. Racing it with our own launch is
             destructive — the second dockerd unlinks the systemd socket
             path, then dies on the volume-store flock ("error while
             opening volume store metadata database (…metadata.db):
             timeout"), leaving Docker permanently unreachable in that
             sandbox.
          3. Otherwise (or if the boot-managed daemon never stabilizes),
             stop the systemd engine + kill stragglers so nothing holds
             the flock or the socket path, launch our own ``dockerd`` as
             a detached background process, and poll it ready.
          4. ``chmod 666`` the unix socket so the non-root sandbox
             ``user`` (which E2B uses for ``commands.run``) can reach it
             without sudo.

        (``set_start_cmd`` is not an option for this: E2B validates the
        start command during the template BUILD in an environment that
        already runs a Docker daemon, so a ``dockerd`` start command
        fails the build with "process with PID N is still running".)

        Raises ``TimeoutError`` if no daemon becomes ready within the
        budget — the caller treats this as a session creation failure.
        """
        async def _noop(_data: bytes) -> None:
            pass

        # chmod the socket if it already exists. This handles the case
        # where a template set_start_cmd started dockerd but left the
        # socket root-only (a race between the ready probe and chmod).
        # If the socket doesn't exist yet this is a no-op.
        chmod_early = await sandbox.run_command(
            [
                "sudo", "sh", "-c",
                "[ -S /var/run/docker.sock ]"
                " && chmod 666 /var/run/docker.sock"
                " || true",
            ],
            env={},
            on_stdout=_noop,
            on_stderr=_noop,
        )
        try:
            await asyncio.wait_for(chmod_early.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        # If daemon is already running and accessible, nothing more to do.
        info_check = await sandbox.run_command(
            ["docker", "info"],
            env={},
            on_stdout=_noop,
            on_stderr=_noop,
        )
        try:
            check_code = await asyncio.wait_for(info_check.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            check_code = 1
        if check_code == 0:
            logger.info("sandbox.e2b.docker.daemon_already_running")
            await self._chmod_docker_socket(sandbox)
            return

        # Not serving yet — but a boot-managed daemon may be mid-startup
        # (systemd's docker.service/docker.socket from the template
        # image). If so, adopt it rather than racing it (see docstring).
        pending = await sandbox.run_command(
            [
                "sh", "-c",
                "pgrep -x dockerd >/dev/null 2>&1 && exit 0;"
                " systemctl is-active docker.service docker.socket"
                " 2>/dev/null | grep -qx -e active -e activating"
                " && exit 0;"
                " exit 1",
            ],
            env={},
            on_stdout=_noop,
            on_stderr=_noop,
        )
        try:
            pending_code = await asyncio.wait_for(pending.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pending_code = 1
        if pending_code == 0:
            attempts = await self._poll_docker_ready(
                sandbox, max_polls=_DOCKER_ADOPT_MAX_POLLS,
            )
            if attempts is not None:
                logger.info(
                    "sandbox.e2b.docker.daemon_adopted", attempts=attempts,
                )
                await self._chmod_docker_socket(sandbox)
                return
            logger.warning(
                "sandbox.e2b.docker.boot_daemon_never_stabilized",
                polled_seconds=_DOCKER_ADOPT_MAX_POLLS * _DOCKER_POLL_INTERVAL,
            )

        # No daemon coming up on its own (or the boot-managed one is
        # wedged) — launch our own. First free the volume-store flock
        # and the socket path: stop the systemd engine and kill any
        # straggler dockerd.
        stop = await sandbox.run_command(
            [
                "sh", "-c",
                "sudo systemctl stop docker.socket docker.service"
                " 2>/dev/null;"
                " sudo pkill -x dockerd 2>/dev/null;"
                " sleep 1;"
                " sudo rm -f /var/run/docker.pid /var/run/docker.sock;"
                " true",
            ],
            env={},
            on_stdout=_noop,
            on_stderr=_noop,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

        # Launch flags:
        #   --iptables=false  — required in Firecracker microVMs where
        #                       the kernel may not expose iptables; MCP
        #                       containers still run fine with host-network.
        #   --bridge=none     — skip creating the docker0 bridge (also
        #                       needs iptables). stdio MCP containers use
        #                       ``docker run -i`` only, no network needed.
        launch = await sandbox.run_command(
            [
                "sh", "-c",
                "nohup sudo dockerd"
                " --iptables=false"
                " --bridge=none"
                " -H unix:///var/run/docker.sock"
                " > /tmp/dockerd.log 2>&1 &",
            ],
            env={},
            on_stdout=_noop,
            on_stderr=_noop,
        )
        await asyncio.wait_for(launch.wait(), timeout=10.0)
        logger.info("sandbox.e2b.docker.daemon_launched")

        # Poll until docker info answers several times IN A ROW. A single
        # success is a false-positive readiness signal — a freshly launched
        # dockerd can answer one ``docker info`` and then be momentarily
        # unreachable for the immediately-following ``docker run``.
        attempts = await self._poll_docker_ready(sandbox)
        if attempts is None:
            # Daemon never stabilized — read its log for diagnosis.
            log_chunks: list[bytes] = []

            async def _collect(chunk: bytes) -> None:
                log_chunks.append(chunk)

            log_proc = await sandbox.run_command(
                ["cat", "/tmp/dockerd.log"],
                env={},
                on_stdout=_collect,
                on_stderr=_noop,
            )
            try:
                await asyncio.wait_for(log_proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            dockerd_log = (
                b"".join(log_chunks).decode("utf-8", errors="replace").strip()
                or "(empty)"
            )
            raise TimeoutError(
                f"dockerd did not become ready (stably) within "
                f"{_DOCKER_MAX_POLLS * _DOCKER_POLL_INTERVAL:.0f}s.\n"
                f"dockerd log:\n{dockerd_log}",
            )
        logger.info(
            "sandbox.e2b.docker.daemon_ready",
            attempts=attempts,
            required_consecutive=_DOCKER_READY_CONSECUTIVE,
        )
        await self._chmod_docker_socket(sandbox)

    async def _chmod_docker_socket(self, sandbox: E2BSandboxHandle) -> None:
        """Make the socket world-writable so the MCP command can reach it
        without sudo. The socket lives inside the isolated microVM."""
        async def _noop(_data: bytes) -> None:
            pass

        chmod = await sandbox.run_command(
            ["sudo", "chmod", "666", "/var/run/docker.sock"],
            env={},
            on_stdout=_noop,
            on_stderr=_noop,
        )
        try:
            await asyncio.wait_for(chmod.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    async def _poll_docker_ready(
        self,
        sandbox: E2BSandboxHandle,
        *,
        max_polls: int = _DOCKER_MAX_POLLS,
        poll_interval: float = _DOCKER_POLL_INTERVAL,
        required_consecutive: int = _DOCKER_READY_CONSECUTIVE,
    ) -> int | None:
        """Poll ``docker info`` until it succeeds *required_consecutive* times
        in a row. Returns the probe count at the point readiness was
        confirmed, or ``None`` if the daemon never stabilized within
        ``max_polls``.

        Requiring consecutive successes (not just one) is the fix for the
        flaky docker-MCP startup: a freshly launched ``dockerd`` can answer a
        single ``docker info`` and then be briefly unreachable for the
        immediately-following ``docker run``. ``docker info`` here runs as the
        same non-root sandbox user the MCP ``docker run`` will, so a streak of
        successes is a strong proxy for "the next ``docker run`` will connect".
        Any failure resets the streak.
        """
        async def _noop(_data: bytes) -> None:
            pass

        consecutive = 0
        for attempt in range(max_polls):
            info = await sandbox.run_command(
                ["docker", "info"],
                env={},
                on_stdout=_noop,
                on_stderr=_noop,
            )
            try:
                exit_code = await asyncio.wait_for(info.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # docker info hung (e.g. daemon mid-start); not ready.
                exit_code = 1
            if exit_code == 0:
                consecutive += 1
                if consecutive >= required_consecutive:
                    return attempt + 1
            else:
                consecutive = 0
            await asyncio.sleep(poll_interval)
        return None

    async def _materialize_files(
        self,
        *,
        sandbox: E2BSandboxHandle,
        upstream_id: str,
        materialize_files: Sequence[MaterializeFile] | None,
    ) -> None:
        """Pre-exec hook: write the upstream's Sandbox files into the
        live sandbox before the MCP process starts.

        Each file is written via the SDK's ``files.write`` primitive
        with mode 0600. Parent directories are created implicitly by
        the SDK. ``target_path`` is the resolved absolute path
        (system Variables already substituted by the caller in
        :class:`UpstreamClientManager._resolve_upstream_template_vars`).

        Failures are fatal — a missing file at MCP-launch time is
        almost always going to surface later as a confusing
        "credentials not found" error from the MCP itself, so it's
        better to fail-fast here with a specific log line. The
        exception propagates out to the caller and surfaces as a
        connection failure on the dashboard with the SDK's raw
        message attached.

        ``contents`` is plaintext and may include credentials — do
        NOT log the body. The file's logical name + resolved path
        + size are safe.
        """
        if not materialize_files:
            return
        # ``session_id`` is ignored by ``sandbox_home`` on E2B (the
        # container home is fixed at ``/home/user``).
        home = self.sandbox_home(session_id="")
        for entry in materialize_files:
            # Confine the operator-controlled target_path to the sandbox
            # home before writing — a ``..`` traversal or an absolute
            # system path that escapes ``${HOME}`` is rejected (SBX-11).
            confined_path = confine_to_sandbox_home(entry.target_path, home)
            size = len(entry.contents.encode("utf-8"))
            logger.info(
                "sandbox.e2b.materialize_file",
                upstream_id=upstream_id,
                name=entry.name,
                target_path=confined_path,
                size_bytes=size,
            )
            try:
                await sandbox.write_file(
                    path=confined_path,
                    contents=entry.contents,
                    mode=0o600,
                )
            except E2BSDKError as exc:
                logger.warning(
                    "sandbox.e2b.materialize_file.failed",
                    upstream_id=upstream_id,
                    name=entry.name,
                    target_path=entry.target_path,
                    error=str(exc),
                )
                raise

    async def _open_sandbox(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        resources: SandboxResources,
        resume_from: SnapshotRef | None,
    ) -> E2BSandboxHandle:
        if resume_from is not None:
            if resume_from.provider != "e2b":
                raise ValueError(
                    f"E2B service got snapshot from"
                    f" provider={resume_from.provider!r}",
                )
            logger.info(
                "sandbox.e2b.resume", org_id=org_id, upstream_id=upstream.id,
                snapshot_id=resume_from.snapshot_id,
            )
            # Volume mounts established at create-time persist across
            # pause/resume — the SDK does not accept volume_mounts on
            # connect. Just reattach to the snapshot; the same volume
            # is still mounted at PERSISTENT_VOLUME_MOUNT_PATH.
            return await self._client.connect_sandbox(resume_from.snapshot_id)

        cfg = upstream.stdio
        assert cfg is not None  # guarded above
        language = language_for_command(cfg.command)
        if language is None:
            raise ResourcesUnsupported(
                "cpu_vcpus",
                cfg.command,
                allowed=(
                    "npx", "uvx", "uv", "node", "python", "python3", "docker",
                ),
            )
        template = self._grid.template_name(
            language=language,
            cpu_vcpus=resources.cpu_vcpus,
            memory_mb=resources.memory_mb,
        )
        metadata: dict[str, str] = {
            "mcpolis_org": org_id,
            "mcpolis_upstream": upstream.id,
            "mcpolis_instance": self._mcpolis_instance,
        }
        # Resolve (or provision) the volume id when the upstream has
        # opted in to persistent storage AND the operator has enabled
        # the Volumes feature account-side. Mismatch between the two
        # (e.g. flag still False but a persisted upstream has
        # ``persistent_disk_enabled=True`` from before the flip)
        # silently boots without a volume; the upstream still works,
        # just without persistence — better than failing-create on
        # an account that doesn't have volumes enabled yet.
        volume_mounts: dict[str, str] | None = None
        if (
            cfg.persistent_disk_enabled
            and self._persistence is not None
            and self._volumes_enabled
        ):
            volume_id = await self._resolve_or_create_volume(
                org_id=org_id, upstream_id=upstream.id,
            )
            volume_mounts = {PERSISTENT_VOLUME_MOUNT_PATH: volume_id}
        elif cfg.persistent_disk_enabled and not self._volumes_enabled:
            logger.warning(
                "sandbox.e2b.persistent_disk.disabled_account_side",
                org_id=org_id, upstream_id=upstream.id,
            )
        logger.info(
            "sandbox.e2b.create", org_id=org_id, upstream_id=upstream.id,
            template=template,
            volume_mounts=volume_mounts,
        )
        return await self._client.create_sandbox(
            template=template,
            metadata=metadata,
            timeout_seconds=self._on_timeout_seconds,
            volume_mounts=volume_mounts,
        )

    async def _resolve_or_create_volume(
        self, *, org_id: str, upstream_id: str,
    ) -> str:
        """Return the E2B volume id for ``(org, upstream)``.

        If a previous session already provisioned one (recorded in
        ``SandboxPersistedRef.metadata[VOLUME_METADATA_KEY]``), return
        it as-is. Otherwise call ``client.create_volume`` and write
        the new id back through the persistence layer so subsequent
        sessions pick up the same volume.

        Caller must have already verified ``self._persistence`` is
        non-``None``.
        """
        assert self._persistence is not None
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream_id,
        )
        if existing is not None:
            volume_id = existing.metadata.get(VOLUME_METADATA_KEY)
            if volume_id:
                return volume_id

        # Volume name carries enough context to identify the owner in
        # the E2B dashboard. The instance id is included so multiple
        # mcpolis backends sharing one E2B account don't clobber each
        # other's volume names (the SDK accepts duplicates but the
        # dashboard is harder to read).
        name = f"mcpolis-{self._mcpolis_instance[:8]}-{org_id}-{upstream_id}"
        volume_id = await self._client.create_volume(name=name)
        logger.info(
            "sandbox.e2b.volume.created",
            org_id=org_id, upstream_id=upstream_id,
            volume_id=volume_id, name=name,
        )

        # Merge into existing metadata if present so we don't blow
        # away an in-flight ``e2b_volume_id`` race or any future
        # metadata key that lives alongside the volume id.
        merged_metadata: dict[str, str] = (
            dict(existing.metadata) if existing is not None else {}
        )
        merged_metadata[VOLUME_METADATA_KEY] = volume_id
        ref = SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=upstream_id,
            mcpolis_instance=self._mcpolis_instance,
            sandbox_id=existing.sandbox_id if existing is not None else None,
            paused_snapshot_id=(
                existing.paused_snapshot_id if existing is not None else None
            ),
            pid=existing.pid if existing is not None else None,
            metadata=merged_metadata,
            cached_server_info=(
                existing.cached_server_info if existing is not None else None
            ),
            cached_self_description=(
                existing.cached_self_description
                if existing is not None else None
            ),
            last_updated=datetime.now(tz=timezone.utc),
        )
        await self._persistence.upsert(ref)
        return volume_id

    # ---------- preserve-on-shutdown hook ----------

    def mark_session_preserve_on_close(self, session_id: str) -> None:
        """Mark a live session so its teardown skips ``sandbox.kill()``.

        Called by the lifespan handler on graceful shutdown for every
        active session (see :meth:`active_session_ids`). The next
        ``_session_cm`` exit then leaves the underlying E2B sandbox
        running so the next backend boot can reattach via
        :meth:`_try_reconnect`.

        No-op if ``session_id`` isn't currently live (the session may
        have already exited; nothing to mark). Safe to call any number
        of times.
        """
        if session_id in self._live_sandboxes:
            self._preserve_on_close[session_id] = True

    def mark_all_active_sessions_preserve_on_close(self) -> int:
        """Bulk variant of :meth:`mark_session_preserve_on_close` for
        every currently-live session. Returns the count marked so the
        lifespan handler can log a single "preserved N sandboxes for
        reconnect" line.
        """
        for sid in list(self._live_sandboxes):
            self._preserve_on_close[sid] = True
        return len(self._live_sandboxes)

    def active_session_ids(self) -> list[str]:
        """Snapshot of session ids with a live sandbox right now.

        For operator tooling and the lifespan shutdown hook. Returns a
        list (not the live dict view) so callers can iterate without
        racing the next ``_session_cm`` exit.
        """
        return list(self._live_sandboxes)

    # ---------- pause + map_exit ----------

    async def wipe_for_fresh_restart(self) -> int:
        """Tear down every persisted live ref + its E2B sandbox.

        Hook for the ``MCPOLIS_E2B_FRESH_SANDBOXES=true`` operator
        override: at startup, before
        ``_connect_all_orgs_background`` runs, the operator can ask
        for a clean slate. This method:

        1. Reads every ``SandboxPersistedRef`` (cross-org).
        2. For each ref with a live ``sandbox_id``, calls
           ``Sandbox.kill(sandbox_id)`` (best-effort; 404s are
           swallowed since the sandbox may already be gone).
        3. Deletes the ref so the next ``_try_reconnect`` finds
           nothing and falls through to fresh-create.

        Returns the count of refs cleared. Idempotent — running it
        twice with no refs left is a no-op.

        Cross-org by design: the operator override is meant for
        "wipe my whole instance back to a clean slate". A per-org
        variant would land separately if a use case appears.
        """
        if self._persistence is None:
            logger.info(
                "sandbox.e2b.fresh_restart.skipped_no_persistence",
            )
            return 0
        refs = await self._persistence.list_all_unscoped()
        cleared = 0
        for ref in refs:
            if ref.provider != "e2b":
                continue
            if ref.sandbox_id is not None:
                try:
                    await self._client.kill_sandbox(ref.sandbox_id)
                    logger.info(
                        "sandbox.e2b.fresh_restart.killed",
                        org_id=ref.org_id, upstream_id=ref.upstream_id,
                        sandbox_id=ref.sandbox_id,
                    )
                except E2BNotFoundError:
                    # Already gone (24h cap, manual delete in
                    # dashboard, etc.). No-op for the wipe.
                    pass
                except E2BSDKError:
                    logger.warning(
                        "sandbox.e2b.fresh_restart.kill_failed",
                        sandbox_id=ref.sandbox_id, exc_info=True,
                    )
            try:
                await self._persistence.delete(
                    org_id=ref.org_id, upstream_id=ref.upstream_id,
                )
                cleared += 1
            except Exception:
                logger.warning(
                    "sandbox.e2b.fresh_restart.persistence_delete_failed",
                    org_id=ref.org_id, upstream_id=ref.upstream_id,
                    exc_info=True,
                )
        logger.info(
            "sandbox.e2b.fresh_restart.done",
            cleared=cleared,
        )
        return cleared

    async def pause(self, session_id: str) -> SnapshotRef | None:
        """Snapshot the live sandbox registered under ``session_id``.

        Returns ``None`` if no session is registered (caller had
        nothing to pause — race with session exit, or pause asked
        for a session that never opened). Otherwise calls
        ``handle.pause()`` on the registered sandbox and returns the
        snapshot ref the caller should persist.
        """
        sandbox = self._live_sandboxes.get(session_id)
        if sandbox is None:
            logger.info(
                "sandbox.e2b.pause.no_live_session",
                session_id=session_id,
            )
            return None
        try:
            snapshot_id = await sandbox.pause()
        except E2BSDKError:
            logger.warning(
                "sandbox.e2b.pause.failed",
                session_id=session_id, exc_info=True,
            )
            return None
        # Pause renders the handle unusable for run_command — drop
        # the registration so a subsequent pause() doesn't reuse a
        # dead handle. The live session() context will also pop on
        # exit; double-pop is a no-op.
        self._live_sandboxes.pop(session_id, None)
        # Mark the session as paused so the session() finally block
        # skips the default ``sandbox.kill()`` cleanup — killing a
        # paused sandbox destroys the snapshot we just created.
        self._paused_sessions.add(session_id)
        logger.info(
            "sandbox.e2b.pause.ok",
            session_id=session_id, snapshot_id=snapshot_id,
        )
        return SnapshotRef(
            provider="e2b", snapshot_id=snapshot_id,
            metadata={"original_sandbox_id": sandbox.sandbox_id},
        )

    async def on_upstream_removed(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        """Destroy the E2B volume for ``(org, upstream)`` when the
        operator deletes the upstream.

        Idempotent: a missing persistence ref, a missing volume id in
        the metadata, or an :class:`E2BNotFoundError` from the SDK
        all resolve to "nothing to clean up." After successful
        teardown, the persistence ref is deleted so the reconciler
        doesn't see a phantom mapping.
        """
        if self._persistence is None:
            return
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream_id,
        )
        if existing is None:
            return
        volume_id = existing.metadata.get(VOLUME_METADATA_KEY)
        if volume_id:
            try:
                await self._client.destroy_volume(volume_id)
                logger.info(
                    "sandbox.e2b.volume.destroyed",
                    org_id=org_id, upstream_id=upstream_id,
                    volume_id=volume_id,
                )
            except E2BNotFoundError:
                # Volume already gone (manual delete in dashboard,
                # or a prior partial teardown). Treat as success so
                # the persistence ref still gets cleared and a retry
                # of the operator action is a no-op.
                logger.info(
                    "sandbox.e2b.volume.destroy_not_found",
                    org_id=org_id, upstream_id=upstream_id,
                    volume_id=volume_id,
                )
            except E2BSDKError:
                logger.warning(
                    "sandbox.e2b.volume.destroy_failed",
                    org_id=org_id, upstream_id=upstream_id,
                    volume_id=volume_id, exc_info=True,
                )
                # Don't clear the ref — leave the volume id behind so
                # a retry can complete teardown. The operator will
                # see the volume in their E2B dashboard until then.
                return
        try:
            await self._persistence.delete(
                org_id=org_id, upstream_id=upstream_id,
            )
        except Exception:
            logger.warning(
                "sandbox.e2b.persistence.delete_failed",
                org_id=org_id, upstream_id=upstream_id, exc_info=True,
            )

    async def kill_persisted_session(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        """Kill the live E2B sandbox for ``(org, upstream)`` and clear
        the session-scoped fields of the persistence ref.

        Volume metadata (``VOLUME_METADATA_KEY``) is preserved so the
        next ``_session_cm`` fresh-create path can still reattach the
        operator's persistent ``/data`` disk. When no volume metadata
        is stored on the ref the whole record is deleted.

        Best-effort throughout: a missing ref, a sandbox already gone
        from the E2B side, and any SDK error all resolve to "no-op,
        proceed." The boot reconciler is the eventual-consistency net
        for anything that survives this call.
        """
        if self._persistence is None:
            return
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream_id,
        )
        if existing is None:
            return
        sandbox_id = existing.sandbox_id
        if sandbox_id:
            try:
                await self._client.kill_sandbox(sandbox_id)
                logger.info(
                    "sandbox.e2b.persisted_session.killed",
                    org_id=org_id, upstream_id=upstream_id,
                    sandbox_id=sandbox_id,
                )
            except E2BNotFoundError:
                # Sandbox already gone (24h cap, manual delete, prior
                # partial teardown). Log and continue — the persistence
                # ref still gets reconciled below.
                logger.info(
                    "sandbox.e2b.persisted_session.kill_not_found",
                    org_id=org_id, upstream_id=upstream_id,
                    sandbox_id=sandbox_id,
                )
            except E2BSDKError:
                logger.warning(
                    "sandbox.e2b.persisted_session.kill_failed",
                    org_id=org_id, upstream_id=upstream_id,
                    sandbox_id=sandbox_id, exc_info=True,
                )
        # Preserve volume metadata so the next Start's fresh-create
        # reattaches the same persistent disk; clear sandbox / pid /
        # paused-snapshot so the next ``_try_reconnect`` finds nothing
        # and falls through to fresh-create. When no volume is
        # configured, just drop the whole record — there is no state
        # worth preserving.
        volume_id = existing.metadata.get(VOLUME_METADATA_KEY)
        try:
            if volume_id:
                await self._persistence.upsert(SandboxPersistedRef(
                    provider=existing.provider,
                    org_id=existing.org_id,
                    upstream_id=existing.upstream_id,
                    mcpolis_instance=existing.mcpolis_instance,
                    sandbox_id=None,
                    paused_snapshot_id=None,
                    pid=None,
                    metadata={VOLUME_METADATA_KEY: volume_id},
                    cached_server_info=existing.cached_server_info,
                    cached_self_description=existing.cached_self_description,
                    last_updated=datetime.now(tz=timezone.utc),
                ))
            else:
                await self._persistence.delete(
                    org_id=org_id, upstream_id=upstream_id,
                )
        except Exception:
            logger.warning(
                "sandbox.e2b.persisted_session.persistence_clear_failed",
                org_id=org_id, upstream_id=upstream_id, exc_info=True,
            )

    def map_exit(
        self, raw: ProviderExitInfo,
    ) -> tuple[ExitReason, str | None]:
        detail = raw.raw_message or None
        # Specific categories where the SDK exposes signal.
        if raw.error_class.endswith("AuthError") or raw.error_class == "E2BAuthError":
            return ExitReason.AUTH_FAILED, detail
        if (
            raw.error_class.endswith("QuotaError")
            or raw.error_class.endswith("RateLimitException")
            or raw.error_class == "E2BQuotaError"
        ):
            return ExitReason.ACCOUNT_LIMIT_EXCEEDED, detail
        # Exit code present + non-zero ⇔ the MCP process itself
        # exited (vs. a sandbox-level failure). Surface as a clean
        # SUBPROCESS_EXITED so admins can distinguish "the MCP
        # crashed" from "the provider failed".
        if raw.exit_code is not None and raw.exit_code != 0:
            return ExitReason.SUBPROCESS_EXITED, detail
        # Everything else: the coarse-signal fallback. The detail
        # string is what admins read in the UI.
        return ExitReason.PROVIDER_ERROR, detail


# Quiet pyright on the unused symbols (kept for re-export from the
# package __init__).
_ReadStream = MemoryObjectReceiveStream[SessionMessage | Exception]
_WriteStream = MemoryObjectSendStream[SessionMessage]
_Awaitable = Awaitable
_E2BAuthError = E2BAuthError
_E2BNotFoundError = E2BNotFoundError
_E2BQuotaError = E2BQuotaError


__all__ = [
    "E2BSandboxService",
    "PERSISTENT_VOLUME_MOUNT_PATH",
    "VOLUME_METADATA_KEY",
]
