"""Real E2B SDK adapter implementing :class:`E2BClient`.

Wraps ``e2b.AsyncSandbox`` (v2.x) so :class:`E2BSandboxService`
can drive the actual hosted SaaS. The ``e2b`` package is imported
lazily at construction time so backends running on the
``own-runner`` / ``local-subprocess`` providers can boot without
the optional dependency installed.

API mapping (E2BClient → e2b SDK):

- ``create_sandbox`` → ``AsyncSandbox.create(template, timeout, metadata)``
- ``connect_sandbox`` → ``AsyncSandbox.connect(sandbox_id=…)``. In v2
  the snapshot id IS the sandbox id (pause keeps the id alive),
  so the persisted ``SnapshotRef.snapshot_id`` is the value we
  pass here.
- ``list_sandboxes`` → ``AsyncSandbox.list(query=SandboxQuery(metadata=…))``,
  iterated through the returned paginator.
- ``kill_sandbox`` → ``AsyncSandbox.kill(self)`` via a connected
  handle (the SDK's class-method variant takes per-call api_key
  opts; routing through a freshly-connected handle lets the
  per-key auth path stay uniform).
- ``delete_snapshot`` → ``AsyncSandbox.delete_snapshot(snapshot_id)``.

SDK exceptions (``AuthenticationException``, ``RateLimitException``,
``NotFoundException``) are wrapped as :class:`E2BAuthError` /
:class:`E2BQuotaError` / :class:`E2BNotFoundError` so the service's
``map_exit`` can categorise them. All other ``Exception`` subclasses
become a generic :class:`E2BSDKError`.
"""
from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from mcpolis.adapters.sandbox_e2b.client import (
    E2BAuthError,
    E2BClient,
    E2BNotFoundError,
    E2BProcessHandle,
    E2BQuotaError,
    E2BSandboxHandle,
    E2BSandboxInfo,
    E2BSDKError,
    coerce_metadata,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from e2b import AsyncCommandHandle as _SDKAsyncCommandHandle
    from e2b import AsyncSandbox as _SDKAsyncSandbox

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ---------- Lazy import + exception mapping ----------


def _import_sdk() -> Any:
    """Import the ``e2b`` package at first call.

    Raises ``E2BSDKError`` if the package isn't installed — keeps
    the failure mode aligned with the rest of the SDK error path
    (the service's ``map_exit`` already handles ``E2BSDKError``
    cleanly).
    """
    try:
        import e2b  # noqa: PLC0415
    except ImportError as exc:
        raise E2BSDKError(
            "ImportError",
            "e2b SDK is not installed. Add ``e2b`` to "
            "requirements.txt and reinstall the conda env.",
        ) from exc
    return e2b


# Envd-readiness probe. After ``Sandbox.connect`` auto-resumes a
# paused sandbox, the gateway returns 201 immediately but the envd
# daemon inside takes ~hundreds of ms (and occasionally many seconds
# for heavy MCP processes) to rebind its port. Without an explicit
# wait, the next ``commands.connect``/``files``/etc. call hits a
# 502 from the gateway and the SDK turns it into a misleading
# ``TimeoutException`` — observed in prod against
# ``server-everything`` but not against ``mcp-fetch``.
ENVD_READY_TIMEOUT_SECONDS: float = 30.0
ENVD_READY_POLL_INTERVAL_SECONDS: float = 0.5
ENVD_HEALTH_REQUEST_TIMEOUT_SECONDS: float = 2.0

# Bound on establishing the streaming RPC for an existing pid
# (``commands.connect``). When envd's port for the persisted pid is
# wedged after auto_resume (observed against ``server-everything``:
# "sandbox is running but port is not open"), the connect hangs ~60 s
# before failing. The SDK's ``request_timeout`` does NOT bound this —
# per e2b-dev #1128 it only sets connect+pool timeouts on streaming
# calls, not a read timeout — so a wedged establish runs to the
# gateway's ~60 s ceiling. We therefore wrap the call in
# ``asyncio.wait_for`` (see ``connect_command``) to actually cap it.
# 10 s keeps the happy path well clear (typical reattach is
# sub-second) while shrinking a stuck reattach from ~60 s to ~10 s,
# after which the caller falls back to a fresh create.
COMMANDS_CONNECT_REQUEST_TIMEOUT_SECONDS: float = 10.0


async def _wait_for_envd_ready(
    sandbox: Any, sandbox_id: str,
    *,
    deadline_seconds: float = ENVD_READY_TIMEOUT_SECONDS,
) -> None:
    """Poll the sandbox's envd ``/health`` until it responds 200 OK.

    Returns once envd is reachable. Raises :class:`E2BSDKError` if
    the deadline expires before envd comes up — signalling to the
    caller (typically ``connect_sandbox``) that this sandbox is
    unusable and a fresh-create fall-back is the right next step.

    Implementation: ``sandbox.is_running()`` already does the right
    HTTP probe (returns ``False`` on 502 = "envd not yet bound"),
    but we keep the per-probe ``request_timeout`` short so a stuck
    sandbox doesn't burn the whole deadline on one hung request.
    """
    started = asyncio.get_running_loop().time()
    last_error: Exception | None = None
    # ``was_paused`` distinguishes "sandbox was already running, no
    # wake cost" from "sandbox was paused, just woke up". Cheap to
    # capture: the first probe already runs; we just record whether
    # it was the success that returned us. Lets operators see the
    # auto-resume hit-rate without an extra probe.
    probe_count = 0
    while True:
        try:
            ready = await sandbox.is_running(
                request_timeout=ENVD_HEALTH_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            # ``is_running`` raises on request timeouts — keep polling
            # until our own deadline. Capture the last error for the
            # eventual raise so operators can distinguish stuck-envd
            # from network blip.
            last_error = exc
            ready = False
        probe_count += 1
        if ready:
            elapsed_ms = int(
                (asyncio.get_running_loop().time() - started) * 1000,
            )
            logger.info(
                "sandbox.e2b.envd_ready",
                sandbox_id=sandbox_id,
                elapsed_ms=elapsed_ms,
                was_paused=probe_count > 1,
            )
            return
        elapsed = asyncio.get_running_loop().time() - started
        if elapsed >= deadline_seconds:
            raise E2BSDKError(
                "TimeoutException",
                (
                    f"envd not reachable on sandbox {sandbox_id!r} "
                    f"after {deadline_seconds:.1f}s "
                    f"(last error: {last_error!r})"
                ),
            )
        await asyncio.sleep(ENVD_READY_POLL_INTERVAL_SECONDS)


def _wrap_sdk_error(exc: Exception) -> E2BSDKError:
    """Map an SDK-side exception into one of our typed wrappers.

    Falls back to a generic ``E2BSDKError`` carrying the exception's
    class name so ``map_exit`` can render the raw message verbatim
    in the admin UI.
    """
    e2b = _import_sdk()
    cls_name = exc.__class__.__name__
    msg = str(exc)
    if isinstance(exc, e2b.AuthenticationException):
        return E2BAuthError(cls_name, msg)
    if isinstance(exc, e2b.RateLimitException):
        return E2BQuotaError(cls_name, msg)
    if isinstance(exc, e2b.NotFoundException):
        return E2BNotFoundError(cls_name, msg)
    return E2BSDKError(cls_name, msg)


# ---------- Adapters that satisfy the Protocol shapes ----------


class _RealSandboxInfo:
    """Adapts the SDK's ``SandboxInfo`` dataclass to our Protocol."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    @property
    def sandbox_id(self) -> str:
        return str(self._raw.sandbox_id)

    @property
    def state(self) -> str:
        # SDK returns SandboxState.RUNNING / .PAUSED enum members.
        # Our Protocol takes a free-form string; pass through .value.
        st = getattr(self._raw, "state", None)
        if st is None:
            return ""
        return str(getattr(st, "value", st))

    @property
    def metadata(self) -> dict[str, str]:
        raw_meta: Any = getattr(self._raw, "metadata", {}) or {}
        if not isinstance(raw_meta, dict):
            return {}
        return coerce_metadata(raw_meta)  # type: ignore[arg-type]

    @property
    def created_at(self) -> datetime:
        # SDK field is ``started_at`` (datetime). Fall back to
        # epoch-zero so the reconciler's age check treats unknown
        # creation times as "old enough to GC if past threshold".
        raw_ts: Any = getattr(self._raw, "started_at", None)
        if isinstance(raw_ts, datetime):
            return raw_ts
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


class _RealCommandHandle:
    """Adapts ``e2b.AsyncCommandHandle`` to our Protocol.

    The SDK exposes ``stdin`` via ``sandbox.commands.send_stdin(pid,
    data)`` rather than a method on the handle itself, so this
    adapter holds a reference to the parent sandbox to route calls.
    """

    def __init__(
        self,
        sandbox: "_SDKAsyncSandbox",
        handle: "_SDKAsyncCommandHandle",
    ) -> None:
        self._sandbox = sandbox
        self._handle = handle

    @property
    def pid(self) -> int:
        return int(self._handle.pid)

    async def send_stdin(self, data: bytes) -> None:
        try:
            await self._sandbox.commands.send_stdin(
                pid=self._handle.pid,
                data=data.decode("utf-8", errors="replace"),
            )
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc

    async def wait(self) -> int:
        e2b = _import_sdk()
        try:
            result = await self._handle.wait()
        except e2b.CommandExitException as exc:
            # The SDK raises ``CommandExitException`` for any non-zero
            # exit. The exception inherits from ``CommandResult``, so
            # it carries the actual exit code — surface it instead of
            # collapsing into a generic SDK error. Without this, the
            # fail-fast init race reports ``exit_code=None`` ("code
            # unknown") for the most common bogus-command case.
            code = getattr(exc, "exit_code", None)
            return int(code) if code is not None else 1
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        # ``wait`` returns a CommandResult with ``exit_code``.
        code = getattr(result, "exit_code", 0)
        return int(code) if code is not None else 0

    async def release(self) -> None:
        # SDK's ``disconnect`` cancels the events-iteration task on
        # ``AsyncCommandHandle._wait``; that propagates a
        # CancelledError into the ``async for event in self._events``
        # loop, which triggers Python's implicit aclose of the
        # underlying generator (which holds an open httpx stream
        # context manager). Driving this from a known point in the
        # event loop avoids the GC-vs-stream-__aexit__ race that
        # surfaces as
        # ``RuntimeError: athrow(): asynchronous generator is
        # already running`` on stderr.
        #
        # The SDK's ``disconnect`` only calls ``cancel()`` on the
        # task; it doesn't await the cancellation propagation, so
        # if we drop the handle reference immediately the cleanup
        # may still be mid-flight and GC can re-enter the same
        # async generators. We explicitly await the cancelled task
        # here (best-effort, swallowing CancelledError) so that by
        # the time ``release()`` returns, the events generator and
        # everything below it have finished their teardown chain.
        import asyncio  # noqa: PLC0415

        try:
            await self._handle.disconnect()
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        wait_task = getattr(self._handle, "_wait", None)
        if wait_task is not None:
            try:
                await wait_task
            except (asyncio.CancelledError, Exception):
                pass

    async def kill(self) -> None:
        try:
            await self._handle.kill()
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc


class _RealSandboxHandle:
    """Adapts ``e2b.AsyncSandbox`` to our :class:`E2BSandboxHandle`."""

    def __init__(self, sandbox: "_SDKAsyncSandbox") -> None:
        self._sandbox = sandbox

    @property
    def sandbox_id(self) -> str:
        return str(self._sandbox.sandbox_id)

    async def run_command(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> E2BProcessHandle:
        # SDK's commands.run takes a single ``cmd`` string + str-typed
        # callbacks; bridge to our bytes-typed Protocol callbacks.
        cmd_str = shlex.join(argv)

        async def _stdout_cb(text: str) -> None:
            result = on_stdout(text.encode("utf-8", errors="replace"))
            if result is not None:
                await result

        async def _stderr_cb(text: str) -> None:
            result = on_stderr(text.encode("utf-8", errors="replace"))
            if result is not None:
                await result

        try:
            handle = await self._sandbox.commands.run(
                cmd_str,
                background=True,
                envs=dict(env),
                on_stdout=_stdout_cb,
                on_stderr=_stderr_cb,
                # ``stdin=True`` opens a writable stdin pipe so
                # ``commands.send_stdin(pid, data)`` actually delivers
                # JSON-RPC requests to the MCP server. Without this
                # flag the SDK doesn't wire up stdin and ``initialize``
                # hangs forever (the MCP sits waiting for input that
                # has nowhere to land). Surfaced by the e2e smoke
                # test in 2026-04 — server-everything logged
                # "Starting default (STDIO) server..." then never
                # responded.
                stdin=True,
                # Disable the SDK's per-command timeout for stdio MCPs
                # — sessions are long-lived and the wrapping
                # ``SandboxService.session()`` controls lifetime.
                timeout=0,
            )
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        return _RealCommandHandle(self._sandbox, handle)

    async def connect_command(
        self,
        *,
        pid: int,
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> E2BProcessHandle:
        # SDK callbacks receive str; bridge to bytes-typed Protocol
        # callbacks the same way as ``run_command``.
        async def _stdout_cb(text: str) -> None:
            result = on_stdout(text.encode("utf-8", errors="replace"))
            if result is not None:
                await result

        async def _stderr_cb(text: str) -> None:
            result = on_stderr(text.encode("utf-8", errors="replace"))
            if result is not None:
                await result

        try:
            # ``wait_for`` is what actually caps the establish: the SDK's
            # ``request_timeout`` doesn't bound the streaming read (#1128),
            # so a wedged envd port would otherwise hang ~60 s. Capping it
            # here lets the caller fall back to a fresh create fast.
            handle = await asyncio.wait_for(
                self._sandbox.commands.connect(
                    pid=pid,
                    on_stdout=_stdout_cb,
                    on_stderr=_stderr_cb,
                    # Match ``run_command``: the wrapping session
                    # controls lifetime, so don't let the SDK time out
                    # the long-lived streaming connection itself.
                    timeout=0,
                    request_timeout=COMMANDS_CONNECT_REQUEST_TIMEOUT_SECONDS,
                ),
                timeout=COMMANDS_CONNECT_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            # Surface as a typed SDK error so the reconnect path treats it
            # as a failed reattach and fresh-creates (it does NOT mean the
            # sandbox is gone — just that this pid's stream won't establish).
            raise E2BSDKError(
                "TimeoutException",
                "commands.connect did not establish within "
                f"{COMMANDS_CONNECT_REQUEST_TIMEOUT_SECONDS:.0f}s "
                "(envd port not open after resume)",
            ) from exc
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        return _RealCommandHandle(self._sandbox, handle)

    async def pause(self) -> str:
        try:
            await self._sandbox.pause()
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        # In v2 the snapshot id IS the sandbox id; reconnect uses
        # the same id. Returning ``sandbox_id`` keeps the abstract
        # ``SnapshotRef.snapshot_id`` round-trippable through
        # ``connect_sandbox``.
        return str(self._sandbox.sandbox_id)

    async def set_timeout(self, timeout_seconds: int) -> None:
        try:
            await self._sandbox.set_timeout(timeout_seconds)
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc

    async def write_file(
        self,
        *,
        path: str,
        contents: str,
        mode: int = 0o600,
    ) -> None:
        # E2B's ``files.write`` accepts path + str/bytes content and
        # creates parent directories implicitly. ``files.set_permissions``
        # then applies the requested mode — Sandbox files often hold
        # credentials, and the SDK doesn't take a mode in the write
        # call itself. Errors from either call wrap into our typed
        # SDK error so the caller can decide whether to fail-fast or
        # log-and-continue.
        try:
            # SDK's WriteInfo return type is partially-unknown to
            # pyright; we don't read the result and ``await`` is fine
            # against any awaitable.
            await self._sandbox.files.write(path, contents)  # pyright: ignore[reportUnknownMemberType]
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        # Best-effort chmod: a failure here means the file is on disk
        # but world-readable — log via the wrapped SDK error so
        # operators can decide whether to retry. We don't suppress
        # because secret material at the wrong mode is a real
        # problem.
        try:
            # ``path`` is operator-controlled (Sandbox file target_path
            # via ${HOME}/${FILE_NAME} substitution), so it can contain
            # spaces or shell metacharacters. Quote it — an unquoted
            # interpolation both breaks on spaces and is a shell-
            # injection vector. ``mode`` is an int rendered as octal,
            # so it needs no quoting.
            await self._sandbox.commands.run(
                f"chmod {mode:03o} {shlex.quote(path)}",
                timeout=10,
            )
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc

    async def kill(self) -> None:
        try:
            await self._sandbox.kill()
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc


# ---------- Top-level client ----------


class RealE2BClient:
    """Real-SDK :class:`E2BClient` impl.

    Construct with the per-team API key; every SDK call routes
    through ``api_key=…`` opts so the same client can be wrapped
    around multiple keys (BYO-key future). The SDK also reads
    ``E2B_API_KEY`` from the environment as a fallback, but we
    prefer explicit injection so a deployment with both modes
    behaves predictably.
    """

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError(
                "RealE2BClient requires a non-empty api_key",
            )
        self._api_key = api_key
        # Force the lazy import early so a missing dep surfaces at
        # construction time, not on the first session.
        _import_sdk()

    @property
    def _opts(self) -> dict[str, Any]:
        return {"api_key": self._api_key}

    async def create_sandbox(
        self,
        *,
        template: str,
        metadata: dict[str, str],
        timeout_seconds: int,
        volume_mounts: dict[str, str] | None = None,
    ) -> E2BSandboxHandle:
        e2b = _import_sdk()
        # ``lifecycle={"on_timeout": "pause", "auto_resume": True}``
        # turns the SDK's default ``on_timeout="kill"`` into a soft
        # pause: when ``timeout_seconds`` elapses with no API
        # activity, E2B snapshots the sandbox instead of terminating
        # it, and the next API call (typically our stdin send to the
        # MCP process) transparently resumes from the snapshot. The
        # alternative — relying on the kill default — bricks any
        # session that goes idle longer than the timer (the sandbox
        # is gone, mcpolis still thinks it's connected, the next
        # tool call hangs forever waiting for stdout that never
        # comes).
        kwargs: dict[str, Any] = {
            "template": template,
            "timeout": timeout_seconds,
            "metadata": coerce_metadata(metadata),
            "lifecycle": {"on_timeout": "pause", "auto_resume": True},
            **self._opts,
        }
        # Only set ``volume_mounts`` when there's something to attach
        # — passing an empty dict to the SDK is fine but explicitly
        # skipping when there's nothing to mount keeps create-call
        # logs clean and avoids exercising an SDK code path that
        # might behave differently on edge inputs.
        if volume_mounts:
            kwargs["volume_mounts"] = dict(volume_mounts)
        try:
            sandbox = await e2b.AsyncSandbox.create(**kwargs)
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        return _RealSandboxHandle(sandbox)

    async def connect_sandbox(self, snapshot_id: str) -> E2BSandboxHandle:
        e2b = _import_sdk()
        try:
            sandbox = await e2b.AsyncSandbox.connect(
                sandbox_id=snapshot_id, **self._opts,
            )
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        # ``Sandbox.connect`` returns as soon as the gateway has
        # auto-resumed the box, but envd inside takes longer to
        # rebind its port — especially for sandboxes paused for a
        # while or running heavy MCP processes (npx server-everything
        # consistently exceeds the SDK's default 60s ``request_timeout``
        # on ``commands.connect``). Probing ``/health`` here surfaces
        # the readiness gap early and within a controlled deadline,
        # before any envd-bound call (commands, files, filesystem)
        # can hang for a full request_timeout window.
        await _wait_for_envd_ready(sandbox, snapshot_id)
        return _RealSandboxHandle(sandbox)

    async def list_sandboxes(
        self, *, metadata_filter: dict[str, str] | None = None,
    ) -> list[E2BSandboxInfo]:
        e2b = _import_sdk()
        from e2b.sandbox.sandbox_api import (  # noqa: PLC0415
            SandboxQuery,
            SandboxState,
        )

        query: Any = None
        if metadata_filter is not None:
            query = SandboxQuery(
                metadata=dict(metadata_filter),
                # No state filter — the reconciler partitions by
                # state itself.
                state=[SandboxState.RUNNING, SandboxState.PAUSED],
            )
        try:
            paginator = e2b.AsyncSandbox.list(query=query, **self._opts)
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc

        # AsyncSandboxPaginator isn't an async iterator — it exposes
        # ``has_next`` + ``next_items()`` (per the SDK's docstring).
        out: list[E2BSandboxInfo] = []
        try:
            while paginator.has_next:
                page = await paginator.next_items()
                for raw in page:
                    out.append(_RealSandboxInfo(raw))
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        return out

    async def kill_sandbox(self, sandbox_id: str) -> None:
        e2b = _import_sdk()
        try:
            # The SDK exposes a class-method variant of kill that
            # takes a sandbox id. Routing through ``connect`` would
            # require an extra round-trip; the class-method form
            # is one HTTP call.
            await e2b.AsyncSandbox._cls_kill(  # pyright: ignore[reportPrivateUsage]
                sandbox_id=sandbox_id, **self._opts,
            )
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc

    async def delete_snapshot(self, snapshot_id: str) -> None:
        e2b = _import_sdk()
        try:
            await e2b.AsyncSandbox.delete_snapshot(
                snapshot_id=snapshot_id, **self._opts,
            )
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc

    async def create_volume(self, *, name: str) -> str:
        e2b = _import_sdk()
        try:
            volume = await e2b.AsyncVolume.create(name, **self._opts)
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc
        return str(volume.volume_id)

    async def destroy_volume(self, volume_id: str) -> None:
        e2b = _import_sdk()
        try:
            await e2b.AsyncVolume.destroy(volume_id, **self._opts)
        except Exception as exc:
            raise _wrap_sdk_error(exc) from exc


def make_real_e2b_client(*, api_key: str) -> E2BClient:
    """Project-style ``make_X`` builder for the real SDK adapter."""
    return RealE2BClient(api_key=api_key)


__all__ = ["RealE2BClient", "make_real_e2b_client"]
