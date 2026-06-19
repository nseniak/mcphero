"""Sandbox provider abstraction.

Defines the typed boundary every sandbox backend (own-runner, E2B,
local-subprocess) implements. Selection happens above this layer; the
``ClientSession`` consumer below it doesn't care which backend produced
the ``(read_stream, write_stream)`` pair.

See ``internal/plans/currently-mcpolis-runs-stdio-witty-ocean.md`` for the
broader plan; this module defines the central Protocol + types.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol, TextIO, runtime_checkable

from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from mcp.shared.message import SessionMessage
from pydantic import BaseModel, ConfigDict, Field

from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.model.upstream import UpstreamDefinition

ReadStream = MemoryObjectReceiveStream[SessionMessage | Exception]
WriteStream = MemoryObjectSendStream[SessionMessage]
SessionStreams = tuple[ReadStream, WriteStream]

SandboxProviderName = Literal["own-runner", "e2b", "local-subprocess"]

# How many trailing bytes of stderr each backend buffers for inclusion
# in fail-fast errors (see ``ExitSignal``). Sized for a small Python
# traceback or a Node startup error without bloating per-session memory.
STDERR_TAIL_BYTES = 4096


class ProcessExitSnapshot(BaseModel):
    """Point-in-time view of the sandboxed process's exit state.

    Returned by ``ExitSignal.snapshot()``. Read from the connection
    task immediately after ``ExitSignal.wait()`` resolves to format the
    user-facing fail-fast error. Safe to call before the process has
    exited; ``exit_code`` is ``None`` in that case.
    """

    model_config = ConfigDict(frozen=True)

    exit_code: int | None = None
    stderr_tail: str = ""


class ExitSignal(Protocol):
    """Per-session signal that the sandboxed subprocess has exited.

    Every backend constructs one of these alongside the session and
    exposes it via ``SandboxSession.exit_signal``. The connection task
    races ``wait()`` against the MCP ``initialize`` handshake — if the
    process dies during the init window we surface a fast, specific
    failure instead of waiting out the full ``INIT_TIMEOUT``.

    Both methods are safe to call from outside the session context.
    """

    async def wait(self) -> None:
        """Block until the subprocess has exited (any code, including
        ``0``). Resolves at most once per session lifetime."""
        ...

    def snapshot(self) -> ProcessExitSnapshot:
        """Capture exit code + recent stderr. Callable any time;
        returns ``exit_code=None`` while the process is still alive."""
        ...


@dataclass(frozen=True)
class MaterializeFile:
    """One file the launcher must write into the sandbox before exec.

    Built per session by the upstream-client layer from the
    :class:`SandboxFileRepository` rows for ``(org_id, upstream_id)``,
    with ``${HOME}`` (and any future system Variable) already resolved
    in ``target_path``. Backends that can't materialize files (e.g.
    ``local-subprocess``) ignore the parameter — see
    :meth:`SandboxService.session`.

    The contents string is plaintext; do NOT log it. The launcher
    writes the file with mode 0600 (owner read/write only) and
    creates parent directories as needed.
    """

    name: str
    contents: str
    target_path: str


@dataclass(frozen=True)
class SandboxSession:
    """Object yielded by ``SandboxService.session(...)``.

    Bundles the JSON-RPC streams (matching the shape produced by
    ``mcp.client.stdio.stdio_client``) with the per-session exit
    signal. A dataclass rather than a Pydantic model so the anyio
    stream types pass through without ``arbitrary_types_allowed``.
    """

    read_stream: ReadStream
    write_stream: WriteStream
    exit_signal: ExitSignal
    # Set by the backend when the session's transport has FATALLY
    # failed (sandbox gone, reattach/stdin send unrecoverable) — as
    # opposed to a transient auto-pause the pump reattaches through.
    # The connection task reads it via ``is_transport_alive()`` so the
    # manager reconnects a dead shared session instead of reusing the
    # zombie (which raises ``BrokenResourceError`` on the next send).
    # ``None`` for backends that don't track it (the session is then
    # always treated as alive — current behaviour).
    transport_failed: asyncio.Event | None = None


class SandboxResources(BaseModel):
    """Per-MCP CPU / RAM / disk request.

    Stored on ``UpstreamDefinition.stdio`` (added in a later step).
    Validated against the active provider's ``SandboxCapabilities`` at
    save time and again at session start; an out-of-range value never
    reaches a real ``Sandbox.create()`` call.
    """

    model_config = ConfigDict(frozen=True)

    cpu_vcpus: float = Field(gt=0)
    memory_mb: int = Field(gt=0)
    # 0 ⇔ provider default / ephemeral. The own runner maps non-zero
    # values to a Phase D loopback ext4 image; E2B's storage is fixed
    # at template-build time so the field is informational there.
    disk_gb: int = Field(ge=0)
    pids_limit: int | None = None


class SandboxResourceCombo(BaseModel):
    """One valid (cpu, ram, disk) triple a provider supports.

    Drives the admin UI's combined picker — listing every triple as a
    single dropdown entry guarantees the operator can never select an
    unsupported combination (E2B, in particular, ships paired CPU/RAM
    templates: ``2 vCPU`` only pairs with ``2048 MiB`` or ``4096 MiB``,
    not ``1024 MiB``).
    """

    model_config = ConfigDict(frozen=True)

    cpu_vcpus: float
    memory_mb: int
    disk_gb: int


class SandboxCapabilities(BaseModel):
    """What this provider can do.

    Drives both UI constraints (admin form only offers compatible
    combinations) and server-side validation (
    ``SandboxService.validate_resources`` rejects anything off-grid).
    """

    model_config = ConfigDict(frozen=True)

    provider: SandboxProviderName

    # Allowed values for the discrete dimensions. Ordered.
    #
    # These per-axis lists drive provider-side validation that treats
    # each dimension independently (the local-subprocess backend). The
    # combined picker on the admin UI reads ``allowed_combinations``
    # instead, so paired-grid backends (E2B) can constrain the form to
    # supported (cpu, ram) tuples without surfacing an off-grid combo.
    allowed_cpu_vcpus: tuple[float, ...]
    allowed_memory_mb: tuple[int, ...]
    # Empty tuple ⇔ disk is not user-configurable on this provider
    # (e.g. E2B fixes storage at template build time). The UI hides
    # the disk axis from the combined-picker label in that case.
    allowed_disk_gb: tuple[int, ...]

    # Authoritative list of valid (cpu, ram, disk) triples. Every
    # provider populates this; the frontend's combined dropdown
    # renders one entry per combo so an off-grid combination is
    # unselectable. For backends with independent-axis validation
    # (local-subprocess) this is the cross-product of the three
    # per-axis lists; for paired-grid backends (E2B) it's the
    # explicit template matrix.
    allowed_combinations: tuple[SandboxResourceCombo, ...]

    # Whether the backend actually enforces the requested CPU/RAM/disk.
    # ``local-subprocess`` spawns the MCP as an ordinary host process and
    # ignores the picked combo, so the admin UI hides/disables the
    # resource picker (with a "not enforced" note) when this is False.
    enforces_resources: bool = True

    supports_pause_resume: bool
    supports_egress_filtering: bool
    supports_persistent_disk: bool


class SnapshotRef(BaseModel):
    """Opaque reference to a paused-sandbox snapshot.

    Returned by ``SandboxService.pause`` and consumed by
    ``SandboxService.session(resume_from=...)``. The ``provider`` field
    is mandatory so a snapshot taken on one backend can never be passed
    by mistake to another.
    """

    model_config = ConfigDict(frozen=True)

    provider: SandboxProviderName
    snapshot_id: str
    # Free-form provider metadata (e.g. E2B template id used at create
    # time). Persisted alongside ``snapshot_id``; not interpreted by
    # callers.
    metadata: dict[str, str] = Field(default_factory=dict)


class ProviderExitInfo(BaseModel):
    """Raw exit signal from a provider, pre-mapping.

    Backends with rich exit information (the own runner, which owns its
    own cgroups + Envoy + image-pull machinery) populate the typed
    fields. Backends with coarse signal (E2B SDK errors) often fill only
    ``raw_message`` + ``error_class``; ``map_exit`` is responsible for
    turning those into a useful ``ExitReason``.
    """

    model_config = ConfigDict(frozen=True)

    # Process exit code, if known. ``None`` for create-time failures
    # that never spawned a process.
    exit_code: int | None = None
    # SDK-specific error class name (e.g. ``"AuthenticationException"``,
    # ``"RateLimitException"``). Empty string when not applicable.
    error_class: str = ""
    # Human-readable message lifted verbatim from the provider. Surfaced
    # to operators via ``Exit.detail`` so the admin UI can render the
    # raw text whenever the enum is too coarse to be useful.
    raw_message: str = ""


class ResourcesUnsupported(ValueError):
    """Raised by ``SandboxService.validate_resources`` for off-grid input.

    The ``field`` attribute is one of ``"cpu_vcpus"`` / ``"memory_mb"``
    / ``"disk_gb"`` / ``"pids_limit"`` so the admin API can report
    which control was out of range.
    """

    def __init__(
        self,
        field: str,
        value: object,
        allowed: Sequence[object] | None = None,
    ) -> None:
        msg = (
            f"sandbox resource {field}={value!r} is not supported"
            + (f"; allowed: {list(allowed)!r}" if allowed is not None else "")
        )
        super().__init__(msg)
        self.field: str = field
        self.value: object = value
        self.allowed: tuple[object, ...] | None = (
            tuple(allowed) if allowed is not None else None
        )


@runtime_checkable
class SandboxService(Protocol):
    """The boundary every sandbox backend implements.

    Implementations live under ``adapters/`` (the E2B impl wraps the
    E2B SDK; the local-subprocess impl spawns the MCP via
    ``mcp.client.stdio.stdio_client``). Selection happens via
    ``SandboxResolver`` + a dict registered at startup.
    """

    name: SandboxProviderName

    def capabilities(self) -> SandboxCapabilities:
        """Return what this provider can do, including the resource
        grids the admin UI should offer."""
        ...

    def validate_resources(self, resources: SandboxResources) -> None:
        """Raise ``ResourcesUnsupported`` if ``resources`` cannot be
        honored by this provider. Successful return ⇔ ``session()``
        will accept the same value without another check."""
        ...

    def sandbox_home(self, *, session_id: str) -> str:
        """Absolute ``$HOME`` the MCP process spawned for ``session_id``
        will actually get.

        This is the value ``${HOME}`` must substitute to so a
        ``${HOME}``-templated ``target_path`` (and any ``${HOME}`` in
        env / headers) lands where the process looks. The contract is
        an EQUALITY: ``session()`` MUST spawn the process with exactly
        this ``$HOME``, and the caller substitutes ``${HOME}`` with it
        before materializing files — so substitution, materialization,
        and the live process all agree.

        E2B returns the container's fixed ``/home/user`` (``session_id``
        ignored). ``local-subprocess`` returns a per-session isolated
        temp dir on the host (keyed on ``session_id``) so the spawned
        process never inherits — or pollutes — the operator's real home.
        Deterministic in ``session_id`` so the manager can derive it for
        substitution and the session can recompute the identical value.
        """
        ...

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
        """Open a sandboxed stdio session.

        The yielded ``SandboxSession.read_stream`` /
        ``write_stream`` pair matches the shape produced by
        ``mcp.client.stdio.stdio_client`` so a wrapping
        ``ClientSession`` works unchanged regardless of which backend
        is in use. ``SandboxSession.exit_signal`` lets the connection
        task race the init handshake against subprocess death.

        ``resume_from`` ⇔ caller wants to resume from a previously
        captured snapshot. Backends without pause/resume MUST raise
        ``NotImplementedError`` if a non-None ``resume_from`` is passed.

        ``errlog`` is a write-only TextIO sink for the sandboxed
        process's stderr. The upstream wrapper passes its
        ``LogBuffer`` (also a ``TextIO``); backends with native
        callback APIs (E2B's ``on_stderr``) bridge bytes through to
        this writer. ``None`` ⇔ stderr is dropped on the floor.

        ``extra_env`` carries per-session env vars that don't live
        on the static upstream config (typically ``MCP_AUTH_TOKEN``
        when the upstream uses bearer-token auth). Merged on top of
        ``upstream.stdio.env``; per-session keys win on collision.

        ``materialize_files`` carries Sandbox-files entries the
        launcher must write into the sandbox before exec. Each entry
        carries the resolved absolute ``target_path`` (system
        Variables already substituted), the plaintext contents, and
        the file's logical name (used for log redaction). Backends
        without a file-write primitive (e.g. ``local-subprocess``,
        which spawns a real subprocess on the host) MAY ignore this
        parameter; the launcher is responsible for mode 0600 +
        parent-directory creation.
        """
        ...

    async def pause(self, session_id: str) -> SnapshotRef | None:
        """Snapshot the running sandbox referenced by ``session_id``.

        ``session_id`` must match a value previously passed to
        :meth:`session` of an active context. Returns ``None`` when
        the backend cannot pause (caller falls back to closing the
        session and cold-starting next time) OR when no live session
        is registered under ``session_id`` (caller has nothing to
        pause). Backends that *can* pause MUST return a non-``None``
        ref for a live registered session.
        """
        ...

    async def on_upstream_removed(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        """Tear down provider-side state attached to an upstream that
        has just been removed by the operator.

        For backends with persistent storage (E2B Volumes, future
        own-runner persistent disks) this is where the storage gets
        destroyed and the persistence ref cleared. Backends without
        persistent storage MUST implement this as a no-op so callers
        can dispatch unconditionally.

        Idempotent: calling it twice for the same upstream MUST NOT
        raise. The operator delete path can race the reconciler; both
        paths should be safe.
        """
        ...

    async def kill_persisted_session(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        """Tear down any persisted live session for this upstream
        without destroying its persistent storage.

        Distinct from :meth:`on_upstream_removed` (which also
        destroys volumes): this is the user-clicked Stop button path,
        where the operator wants the running MCP killed but the
        persistent ``/data`` disk to survive for the next Start.

        Without this, an upstream sitting in DEFERRED_ATTACH state
        (post-boot lazy reattach: cached metadata in memory, live
        sandbox + pid in persistence, but no in-memory connection
        task) would survive a Stop+Start cycle: the
        ``transition_to_disabled`` drain has no task to close, so
        ``_session_cm.finally`` never runs, and the next Start's
        reuse-on-restart path reattaches to the same sandbox /
        process. The existing MCP keeps running unmodified, so the
        Server-logs panel ends up empty (the per-session
        ``LogBuffer.clear()`` runs at session start but no fresh
        install / startup output replaces it).

        Backends without persistence (local-subprocess) MUST
        implement this as a no-op. Idempotent: missing ref / missing
        sandbox both resolve to no-op so callers can dispatch
        unconditionally.
        """
        ...

    def map_exit(self, raw: ProviderExitInfo) -> tuple[ExitReason, str | None]:
        """Translate a provider-native failure into the EXIT enum.

        Coarse-signal backends return
        ``(ExitReason.PROVIDER_ERROR, raw.raw_message)`` for anything
        they can't categorize; the admin UI already renders the detail
        string verbatim, so this preserves operator-actionable info.
        """
        ...


__all__ = [
    "STDERR_TAIL_BYTES",
    "ExitSignal",
    "ProcessExitSnapshot",
    "ProviderExitInfo",
    "ReadStream",
    "ResourcesUnsupported",
    "SandboxCapabilities",
    "SandboxProviderName",
    "SandboxResourceCombo",
    "SandboxResources",
    "SandboxService",
    "SandboxSession",
    "SessionStreams",
    "SnapshotRef",
    "WriteStream",
]
