"""Protocol abstraction over the E2B Python SDK.

``E2BClient`` covers exactly the SDK methods :class:`E2BSandboxService`
calls. Two impls:

- :class:`mcpolis.adapters.sandbox_e2b.real_client.RealE2BClient`
  wraps the actual ``e2b`` package; lazily imported so the backend
  boots in environments where the SDK isn't installed.
- Tests inject a hand-rolled mock implementing this Protocol. The
  service's behaviour is fully exercised against the mock without
  any SDK calls.

Errors raised by the SDK are wrapped in :class:`E2BSDKError` (and
its specific subclasses) so the service can ``map_exit`` them into
``ExitReason`` values without leaking SDK internals through the
``SandboxService`` boundary.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol


class E2BSDKError(Exception):
    """Wraps any provider-side failure for ``E2BSandboxService.map_exit``.

    Carries the ``error_class`` (the SDK's exception class name as a
    string) and a human-readable detail. Specific subclasses below
    cover the categories E2B exposes signal for.
    """

    def __init__(self, error_class: str, detail: str = "") -> None:
        super().__init__(f"{error_class}: {detail}" if detail else error_class)
        self.error_class: str = error_class
        self.detail: str = detail


class E2BAuthError(E2BSDKError):
    """API key invalid / revoked. Maps to ``ExitReason.AUTH_FAILED``."""


class E2BQuotaError(E2BSDKError):
    """Account quota / billing cap hit. Maps to
    ``ExitReason.ACCOUNT_LIMIT_EXCEEDED``."""


class E2BNotFoundError(E2BSDKError):
    """Sandbox / snapshot id not found by the provider. Maps to
    ``ExitReason.PROVIDER_ERROR`` with the raw detail."""


# ---------- Sandbox info ----------


class E2BSandboxInfo(Protocol):
    """Read-only view of a sandbox returned by ``E2BClient.list``."""

    @property
    def sandbox_id(self) -> str: ...

    @property
    def state(self) -> str:
        """Provider-reported state. Values we care about: ``running``,
        ``paused``. Unknown values pass through unchanged."""
        ...

    @property
    def metadata(self) -> dict[str, str]: ...

    @property
    def created_at(self) -> datetime:
        """When the sandbox was created on the provider side. Used by
        the reconciler to GC unknown paused snapshots older than the
        retention threshold (default 30 days)."""
        ...


# ---------- Process handle ----------


class E2BProcessHandle(Protocol):
    """Background process handle returned by
    :meth:`E2BSandboxHandle.run_command`. The service writes stdin via
    :meth:`send_stdin` and waits for termination via :meth:`wait`."""

    @property
    def pid(self) -> int:
        """Process id inside the sandbox. Stable across pause/resume
        in E2B v2 — used by :meth:`E2BSandboxHandle.connect_command`
        to reattach a fresh stdout-streaming RPC after auto-pause
        severed the original one."""
        ...

    async def send_stdin(self, data: bytes) -> None: ...

    async def wait(self) -> int:
        """Block until the process exits. Return the exit code.

        After E2B auto-pauses and resumes the sandbox, the SDK's
        underlying streaming RPC for stdout/stderr is severed and
        not auto-reconnected; ``wait`` returns (or raises) at that
        point even though the process inside the sandbox is still
        alive. The service treats any return from ``wait`` as
        "no more output will arrive on this handle" and reattaches
        via ``connect_command``."""
        ...

    async def release(self) -> None:
        """Tear down the streaming RPC behind this handle without
        killing the process inside the sandbox.

        Called after :meth:`E2BSandboxHandle.connect_command` returns
        a fresh handle to the same pid: the OLD handle's events
        generator is still in memory holding an open httpx stream
        context manager. If left to be aclose'd by GC at an
        arbitrary later point, the cleanup races with the httpx
        stream's own ``__aexit__``, surfacing as
        ``RuntimeError: athrow(): asynchronous generator is already
        running`` somewhere on stderr. Explicitly releasing the
        old handle here drives the cleanup deterministically while
        we still own the loop frame.

        The implementation is the SDK's documented ``disconnect``
        primitive (cancels the events-iteration task, which in
        turn closes the underlying generator). Idempotent; safe
        to call on a handle that's already exited."""
        ...

    async def kill(self) -> None: ...


# ---------- Sandbox handle ----------


class E2BSandboxHandle(Protocol):
    """Live (or paused) sandbox the service operates on."""

    @property
    def sandbox_id(self) -> str: ...

    async def run_command(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> E2BProcessHandle:
        """Start ``argv`` in the background. Stream stdout/stderr to
        the supplied callbacks; return a handle for stdin + wait."""
        ...

    async def connect_command(
        self,
        *,
        pid: int,
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> E2BProcessHandle:
        """Reattach to a still-running process by ``pid`` and rewire
        the stdout/stderr stream callbacks.

        Required because E2B's ``on_timeout=pause`` lifecycle severs
        the streaming RPC behind the original ``run_command`` handle
        when the sandbox snapshots; the SDK does not reconnect it on
        ``auto_resume``. The service detects the dead stream
        (``E2BProcessHandle.wait`` returns) and calls this method to
        get a fresh handle pointed at the same pid. The pid is stable
        across E2B pause/resume in v2."""
        ...

    async def pause(self) -> str:
        """Snapshot the sandbox; return the snapshot id. After pause,
        the handle is no longer usable for ``run_command`` — the
        service must connect to the snapshot to resume."""
        ...

    async def write_file(
        self,
        *,
        path: str,
        contents: str,
        mode: int = 0o600,
    ) -> None:
        """Write ``contents`` to ``path`` inside the sandbox.

        Used by the pre-exec hook in :class:`E2BSandboxService` to
        materialize per-MCP Sandbox files before the MCP process
        starts. ``path`` is absolute (system Variables already
        resolved). Parent directories are created as needed.
        ``mode`` is applied to the file after the write (defaults to
        0600 — owner read/write — because Sandbox files often hold
        credentials)."""
        ...

    async def set_timeout(self, timeout_seconds: int) -> None:
        """Re-apply the sandbox's idle ``on_timeout`` window.

        Required after :meth:`connect_command` triggers an
        ``auto_resume``: E2B resets the timeout to the SDK default
        (``default_sandbox_timeout`` = 300s as of e2b 2.20.x), NOT
        the value passed to ``Sandbox.create``. Verified empirically
        2026-05-01 — see ``backend/tests/integration/diagnose_double_reattach.py``.
        Without this re-application, every auto-pause/reattach cycle
        bumps the idle window back to 300s, defeating the
        ``MCPOLIS_E2B_IDLE_PAUSE_SECONDS`` cost knob for any sandbox
        that's been resumed at least once."""
        ...

    async def kill(self) -> None: ...


# ---------- Top-level client ----------


class E2BClient(Protocol):
    """Top-level entrypoint mirroring the E2B SDK's class-method API.

    Methods raise an :class:`E2BSDKError` (or specific subclass) on
    provider-side failure; the service's ``map_exit`` handles
    classification.
    """

    async def create_sandbox(
        self,
        *,
        template: str,
        metadata: dict[str, str],
        timeout_seconds: int,
        volume_mounts: dict[str, str] | None = None,
    ) -> E2BSandboxHandle:
        """Spawn a fresh sandbox from a template. ``metadata`` should
        carry the mcpolis attribution tags (org, upstream, instance,
        session). ``timeout_seconds`` is the on_timeout=pause backstop
        — see plan §"Resilience".

        ``volume_mounts`` maps in-sandbox mount paths to E2B volume
        ids; the SDK attaches each volume at the requested path.
        ``None`` or empty dict ⇔ no extra volumes mounted (sandbox
        gets only its template-provided storage)."""
        ...

    async def connect_sandbox(self, snapshot_id: str) -> E2BSandboxHandle:
        """Resume from a paused-snapshot id.

        Volumes mounted at create-time persist across pause/resume —
        the SDK does not re-accept volume_mounts on connect."""
        ...

    async def list_sandboxes(
        self, *, metadata_filter: dict[str, str] | None = None,
    ) -> list[E2BSandboxInfo]:
        """List sandboxes attributed to this account. ``metadata_filter``
        is an AND of tag matches; ``None`` ⇔ list everything (the
        reconciler's cross-instance global view)."""
        ...

    async def kill_sandbox(self, sandbox_id: str) -> None: ...

    async def delete_snapshot(self, snapshot_id: str) -> None: ...

    async def create_volume(self, *, name: str) -> str:
        """Provision a fresh persistent volume and return its
        ``volume_id``. The id is opaque to mcpolis; we round-trip it
        as ``SandboxPersistedRef.metadata['e2b_volume_id']`` so a
        subsequent session can mount the same volume.

        ``name`` is human-readable (E2B requires it for listing /
        dashboard); duplicate names are accepted by the provider but
        the service builds names from ``mcpolis_instance + org +
        upstream`` so collisions are unlikely."""
        ...

    async def destroy_volume(self, volume_id: str) -> None:
        """Delete a volume by id. Idempotent — destroying an unknown
        id raises :class:`E2BNotFoundError` which the caller treats
        as a successful teardown."""
        ...


def coerce_metadata(meta: dict[str, Any]) -> dict[str, str]:
    """Coerce a free-form metadata dict to ``dict[str, str]`` defensively.

    The E2B SDK accepts arbitrary stringly-typed metadata; this helper
    is shared by the real-SDK adapter and the test mock so both apply
    the same coercion (and the same protections against accidentally
    storing non-string values that won't survive a round trip).
    """
    out: dict[str, str] = {}
    for k, v in meta.items():
        if v is None:
            continue
        out[k] = str(v)
    return out


__all__ = [
    "coerce_metadata",
    "E2BAuthError",
    "E2BClient",
    "E2BNotFoundError",
    "E2BProcessHandle",
    "E2BQuotaError",
    "E2BSandboxHandle",
    "E2BSandboxInfo",
    "E2BSDKError",
]
