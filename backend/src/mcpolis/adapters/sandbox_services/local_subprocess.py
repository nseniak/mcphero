"""``LocalSubprocessSandboxService`` — no-isolation dev path.

Spawns the MCP server as a plain host subprocess via
:func:`asyncio.create_subprocess_exec` and bridges its stdio into the
JSON-RPC memory streams the wrapping ``ClientSession`` consumes. Used
in dev when ``MCPOLIS_SANDBOX_PROVIDER`` is unset (or explicitly set
to ``local-subprocess``); never selected in cloud / production mode.

Resource constraints declared on this backend are not enforced — the
process runs unconstrained on the host. The grid + validation exist
purely so the abstraction stays consistent across backends; admins
who care about real isolation pick ``e2b``.

Owns the spawn directly (rather than wrapping
``mcp.client.stdio.stdio_client``) so the per-session ``ExitSignal``
can observe the subprocess's exit code, enabling the
``SubprocessExitedDuringInit`` fast-fail path in the connection task.
``stdio_client`` hides the underlying ``anyio.Process`` and offers no
way to detect early exit.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import TextIO

import anyio
import structlog
from mcp import types
from mcp.shared.message import SessionMessage

from mcpolis.adapters.sandbox_services.exit_signal import ExitSignalImpl
from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_service import (  # noqa: I001
    SandboxResourceCombo,
    MaterializeFile,
    ProviderExitInfo,
    ResourcesUnsupported,
    SandboxCapabilities,
    SandboxProviderName,
    SandboxResources,
    SandboxSession,
    SnapshotRef,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Local-subprocess capability grid. The values are not enforced; they
# exist so the contract suite can exercise the validation surface and
# so the admin UI can render a consistent picker on every backend.
_ALLOWED_CPU_VCPUS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
_ALLOWED_MEMORY_MB: tuple[int, ...] = (256, 512, 1024, 2048, 4096, 8192)
_ALLOWED_DISK_GB: tuple[int, ...] = (0,)


class LocalSubprocessSandboxService:
    """No-sandbox stdio path. ``name = "local-subprocess"``."""

    name: SandboxProviderName = "local-subprocess"

    def capabilities(self) -> SandboxCapabilities:
        # Local-subprocess validates each axis independently — every
        # cross-product triple is supported. Materialise the
        # cross-product so the admin UI's combined picker can render
        # one entry per combo, same surface as paired-grid backends.
        combos = tuple(
            SandboxResourceCombo(
                cpu_vcpus=cpu, memory_mb=ram, disk_gb=disk,
            )
            for cpu in _ALLOWED_CPU_VCPUS
            for ram in _ALLOWED_MEMORY_MB
            for disk in _ALLOWED_DISK_GB
        )
        return SandboxCapabilities(
            provider="local-subprocess",
            allowed_cpu_vcpus=_ALLOWED_CPU_VCPUS,
            allowed_memory_mb=_ALLOWED_MEMORY_MB,
            allowed_disk_gb=_ALLOWED_DISK_GB,
            allowed_combinations=combos,
            # The subprocess runs unconstrained on the host — the picked
            # CPU/RAM/disk are advisory only, never enforced.
            enforces_resources=False,
            supports_pause_resume=False,
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
        if resources.disk_gb not in _ALLOWED_DISK_GB:
            raise ResourcesUnsupported(
                "disk_gb", resources.disk_gb, allowed=_ALLOWED_DISK_GB,
            )

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
        # session_id is informational here; this backend has no
        # snapshot machinery to register a handle under.
        _ = session_id, resources, denylist
        if resume_from is not None:
            raise NotImplementedError(
                "local-subprocess does not support pause/resume",
            )
        # ``local-subprocess`` runs the MCP as a real host subprocess
        # so file materialization is a real ``open(path, "w")`` —
        # parent dirs created as needed, mode 0600 to match the E2B
        # path. The operator is in dev-only territory here (the
        # cloud-mode startup validator rejects this backend); files
        # land in the host's filesystem at whatever ``target_path``
        # resolved to. Writes happen synchronously before the
        # subprocess starts so the spawned MCP can read its
        # credentials at startup.
        if materialize_files:
            for f in materialize_files:
                p = Path(f.target_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f.contents, encoding="utf-8")
                p.chmod(0o600)
            logger.info(
                "sandbox.local_subprocess.materialize_files.wrote",
                upstream_id=upstream.id,
                count=len(list(materialize_files)),
            )
        return self._session_cm(
            org_id=org_id,
            upstream=upstream,
            errlog=errlog,
            extra_env=extra_env,
        )

    @asynccontextmanager
    async def _session_cm(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        errlog: TextIO | None,
        extra_env: dict[str, str] | None,
    ) -> AsyncIterator[SandboxSession]:
        cfg = upstream.stdio
        if cfg is None:
            raise ValueError(
                f"upstream {upstream.id!r} has transport=stdio but"
                " stdio config is missing",
            )
        # Merge upstream env on top of the host's so PATH / HOME /
        # locale survive. Without this an operator who supplies any
        # env value at all (the common case) loses the ability to
        # locate ``python3`` / ``npx`` / ``uvx`` on PATH and the
        # spawn fails with ``ModuleNotFoundError`` or "command not
        # found" before init. Operator-provided keys win on collision
        # so they can still scrub a sensitive host var by setting
        # it explicitly.
        spawn_env: dict[str, str] = dict(os.environ)
        spawn_env.update(cfg.env)
        if extra_env:
            spawn_env.update(extra_env)

        logger.info(
            "sandbox.local_subprocess.session.start",
            org_id=org_id,
            upstream_id=upstream.id,
        )

        # Memory streams shaped exactly like
        # ``mcp.client.stdio.stdio_client``'s output — the wrapping
        # ``ClientSession`` consumes them transparently. ``read_stream``
        # carries SessionMessage|Exception (parser failures surface as
        # exceptions on the stream); ``write_stream`` accepts
        # SessionMessages from ClientSession.
        read_writer, read_stream = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        write_stream, write_reader = anyio.create_memory_object_stream[
            SessionMessage
        ](0)

        exit_signal = ExitSignalImpl()

        try:
            process = await asyncio.create_subprocess_exec(
                cfg.command,
                *list(cfg.args),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn_env,
            )
        except FileNotFoundError as exc:
            # The raw OSError is just "[Errno 2] No such file or
            # directory" — it never names the command, so a new user has
            # no way to know e.g. ``npx`` isn't installed. Surface the
            # missing executable explicitly; ``str(exc)`` is what reaches
            # the dashboard's "Couldn't connect" line.
            raise FileNotFoundError(
                f"Command {cfg.command!r} not found in the sandbox "
                "environment. Make sure it's installed and on the "
                "server's PATH."
            ) from exc
        except PermissionError as exc:
            raise PermissionError(
                f"Command {cfg.command!r} is not executable in the "
                "sandbox environment."
            ) from exc
        # Fail-loud type-narrow: PIPE is requested for all three so the
        # SDK populates them. Asserting keeps the rest of the code free
        # of ``| None`` noise on every send/read.
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        async def stdout_pump() -> None:
            buffer = ""
            try:
                assert process.stdout is not None
                while True:
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        break
                    buffer += chunk.decode("utf-8", errors="replace")
                    *complete_lines, leftover = buffer.split("\n")
                    buffer = leftover
                    for line in complete_lines:
                        if not line:
                            continue
                        try:
                            msg = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            # Non-JSON-RPC lines on stdout are
                            # typically install/startup chatter — push
                            # them to the operator's errlog so the
                            # "Server logs" pane isn't silent during
                            # cold installs, while still informing
                            # ClientSession the line was bad.
                            logger.warning(
                                "sandbox.local_subprocess.stdout.parse_failed",
                                error=str(exc), line_preview=line[:120],
                            )
                            if errlog is not None:
                                try:
                                    errlog.write(line + "\n")
                                except Exception:
                                    logger.warning(
                                        "sandbox.local_subprocess.stdout.errlog_write_failed",
                                        exc_info=True,
                                    )
                            try:
                                await read_writer.send(exc)
                            except anyio.ClosedResourceError:
                                return
                            continue
                        try:
                            await read_writer.send(SessionMessage(message=msg))
                        except anyio.ClosedResourceError:
                            return
            finally:
                # EOF on stdout means the process has closed it — most
                # commonly because it exited. Close the write end of
                # the read stream so ClientSession sees the channel
                # closed instead of waiting for the next message.
                await read_writer.aclose()

        async def stderr_pump() -> None:
            try:
                assert process.stderr is not None
                while True:
                    chunk = await process.stderr.read(4096)
                    if not chunk:
                        break
                    # Tee into the exit-signal tail FIRST so a
                    # stderr-then-die race still has the bytes
                    # available when the connection task reads
                    # ``snapshot()``.
                    exit_signal.append_stderr(chunk)
                    if errlog is not None:
                        try:
                            errlog.write(
                                chunk.decode("utf-8", errors="replace"),
                            )
                        except Exception:
                            logger.warning(
                                "sandbox.local_subprocess.stderr.write_failed",
                                exc_info=True,
                            )
            except Exception:  # pragma: no cover - never crash the session
                logger.warning(
                    "sandbox.local_subprocess.stderr.read_failed",
                    exc_info=True,
                )

        async def stdin_pump() -> None:
            try:
                assert process.stdin is not None
                async with write_reader:
                    async for session_message in write_reader:
                        body = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True,
                        )
                        try:
                            process.stdin.write((body + "\n").encode("utf-8"))
                            await process.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError) as exc:
                            # Subprocess died underneath us — surface
                            # via the read stream so any in-flight
                            # call_tool fails fast.
                            try:
                                await read_writer.send(exc)
                            except anyio.ClosedResourceError:
                                pass
                            return
            except anyio.ClosedResourceError:
                return

        async def exit_watcher() -> None:
            exit_code: int | None = None
            try:
                exit_code = await process.wait()
            except BaseException:
                # ``process.wait()`` shouldn't normally raise, but
                # treat any exception as "process is gone" and report
                # exit_code=None ("exited, code unknown").
                pass
            finally:
                exit_signal.mark_exited(exit_code)

        stdout_task = asyncio.create_task(stdout_pump())
        stderr_task = asyncio.create_task(stderr_pump())
        stdin_task = asyncio.create_task(stdin_pump())
        exit_task = asyncio.create_task(exit_watcher())

        try:
            yield SandboxSession(
                read_stream=read_stream,
                write_stream=write_stream,
                exit_signal=exit_signal,
            )
        finally:
            # Closing the write_stream end-cap stops the stdin pump;
            # that flushes pending writes and returns from its async
            # for. Then close the subprocess's stdin so the child sees
            # EOF and (well-behaved MCPs) exits.
            await write_stream.aclose()
            try:
                await asyncio.wait_for(stdin_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stdin_task.cancel()

            if process.returncode is None:
                try:
                    if not process.stdin.is_closing():
                        process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                # Give the child a brief grace window to exit cleanly
                # on its stdin EOF before we SIGKILL. Most MCPs exit
                # near-instantly; bound the wait so a misbehaving
                # server doesn't block teardown.
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "sandbox.local_subprocess.kill_timeout",
                            org_id=org_id,
                            upstream_id=upstream.id,
                        )

            for t in (stdout_task, stderr_task, exit_task):
                if not t.done():
                    t.cancel()
            for t in (stdout_task, stderr_task, exit_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            logger.info(
                "sandbox.local_subprocess.session.end",
                org_id=org_id,
                upstream_id=upstream.id,
                exit_code=process.returncode,
            )

    async def pause(self, session_id: str) -> SnapshotRef | None:
        # Subprocesses on the host can't be snapshotted; caller falls
        # back to close + cold-restart per the SandboxService contract.
        _ = session_id
        return None

    async def on_upstream_removed(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        # No persistent storage on this backend — nothing to tear
        # down. Method exists so callers can dispatch unconditionally.
        _ = org_id, upstream_id

    async def kill_persisted_session(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        # No persistence on this backend — Stop just drops the
        # in-memory task and Start cold-spawns a fresh subprocess.
        # Method exists so callers can dispatch unconditionally.
        _ = org_id, upstream_id

    def map_exit(
        self, raw: ProviderExitInfo,
    ) -> tuple[ExitReason, str | None]:
        # The local subprocess has no provider-specific signal. If the
        # exit code is non-zero we surface ``SUBPROCESS_EXITED`` with
        # the raw message (when present) for the admin UI's free-text
        # render; otherwise we map to ``INTERNAL_ERROR``.
        detail = raw.raw_message or None
        if raw.exit_code is not None and raw.exit_code != 0:
            return ExitReason.SUBPROCESS_EXITED, detail
        return ExitReason.INTERNAL_ERROR, detail


__all__ = ["LocalSubprocessSandboxService"]
