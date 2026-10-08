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
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from enum import Enum
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
from mcpolis.adapters.sandbox_e2b.idle_pause_timer import IdlePauseTimer
from mcpolis.adapters.sandbox_e2b.template_grid import (
    E2BTemplateGrid,
    language_for_command,
)
from datetime import datetime, timezone

from mcpolis.domain.services.background_tasks import BackgroundTaskSet
from mcpolis.domain.services.cancel_shield import (
    TimeLimit,
    finish_despite_cancels,
)
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

# Metadata key recording, in the persisted ref, the ``mcpolis_instance``
# tag the sandbox was created with. E2B metadata is fixed at create time
# and the boot reconcile lists sandboxes by the current tag only, so a
# sandbox carrying any other tag is invisible to it. Reusing one would
# hide it for good (see ``_try_reconnect``).
SANDBOX_INSTANCE_METADATA_KEY: str = "e2b_sandbox_instance"

# Metadata key listing (space-separated) the volumes of a removed
# upstream whose destroy failed. Never mounted: an upstream added again
# under the same id gets a volume of its own. The boot reconcile and a
# later removal of the same id retry the destroys. Nothing else points
# at those volumes (E2B has no listing we use), so the ref keeps them.
VOLUMES_TO_DESTROY_METADATA_KEY: str = "e2b_volumes_to_destroy"

# Metadata key listing (space-separated) the sandboxes whose kill failed
# when nothing else would name them any more: a removed upstream's (an
# org deletion's too), and the ones the fresh-sandboxes override could
# not kill. Never reused. The boot reconcile kills them by id, whatever
# instance tag they carry, and a later removal of the same id retries
# them too. A sandbox made before the instance id became one value per
# database carries a tag the reconcile does not list, so this ref is the
# only thing that still names it.
SANDBOXES_TO_KILL_METADATA_KEY: str = "e2b_sandboxes_to_kill"


def volumes_to_destroy(ref: SandboxPersistedRef) -> list[str]:
    """The volumes a removal of ``ref``'s upstream could not destroy yet."""
    return ref.metadata.get(VOLUMES_TO_DESTROY_METADATA_KEY, "").split()


def sandboxes_to_kill(ref: SandboxPersistedRef) -> list[str]:
    """The sandboxes whose kill failed once nothing else would name them
    (see ``SANDBOXES_TO_KILL_METADATA_KEY``)."""
    return ref.metadata.get(SANDBOXES_TO_KILL_METADATA_KEY, "").split()


def leftovers(
    *, volumes: Sequence[str], sandboxes: Sequence[str],
) -> dict[str, str]:
    """The metadata a ref keeps for what is still to clean up: the
    volumes still to destroy and the sandboxes still to kill, each once."""
    metadata: dict[str, str] = {}
    if volumes:
        metadata[VOLUMES_TO_DESTROY_METADATA_KEY] = " ".join(
            dict.fromkeys(volumes),
        )
    if sandboxes:
        metadata[SANDBOXES_TO_KILL_METADATA_KEY] = " ".join(
            dict.fromkeys(sandboxes),
        )
    return metadata


def without_sandbox(
    ref: SandboxPersistedRef, metadata: dict[str, str],
) -> SandboxPersistedRef | None:
    """``ref`` with no sandbox, process or cached server metadata left,
    keeping only ``metadata``; ``None`` when that is empty: nothing is
    left that needs a ref."""
    if not metadata:
        return None
    return SandboxPersistedRef(
        provider=ref.provider,
        org_id=ref.org_id,
        upstream_id=ref.upstream_id,
        mcpolis_instance=ref.mcpolis_instance,
        sandbox_id=None,
        paused_snapshot_id=None,
        pid=None,
        metadata=metadata,
        cached_server_info=None,
        cached_self_description=None,
        last_updated=datetime.now(tz=timezone.utc),
    )


def storage_only(
    ref: SandboxPersistedRef, *, also_to_kill: Sequence[str] = (),
) -> SandboxPersistedRef | None:
    """What of ``ref`` outlives its sandbox: the persistent volume the
    upstream mounts (its ``/data``, reattached by the next fresh create),
    the volumes still to destroy and the sandboxes still to kill, with
    ``also_to_kill`` added to those. ``None`` when it records none."""
    mounted = ref.metadata.get(VOLUME_METADATA_KEY)
    metadata = {VOLUME_METADATA_KEY: mounted} if mounted else {}
    metadata.update(leftovers(
        volumes=volumes_to_destroy(ref),
        sandboxes=[*sandboxes_to_kill(ref), *also_to_kill],
    ))
    return without_sandbox(ref, metadata)


def with_leftovers_gone(
    ref: SandboxPersistedRef,
    *,
    volumes: Collection[str] = (),
    sandboxes: Collection[str] = (),
) -> SandboxPersistedRef | None:
    """``ref`` once the ``volumes`` are destroyed and the ``sandboxes``
    killed: no longer listed as still to clean up, the rest untouched.
    ``None`` when nothing at all is left on it."""
    metadata = {
        key: value for key, value in ref.metadata.items()
        if key not in (
            VOLUMES_TO_DESTROY_METADATA_KEY, SANDBOXES_TO_KILL_METADATA_KEY,
        )
    }
    metadata.update(leftovers(
        volumes=[v for v in volumes_to_destroy(ref) if v not in volumes],
        sandboxes=[s for s in sandboxes_to_kill(ref) if s not in sandboxes],
    ))
    if (
        not metadata
        and ref.sandbox_id is None
        and ref.paused_snapshot_id is None
        and ref.cached_server_info is None
        and ref.cached_self_description is None
    ):
        return None
    return ref.model_copy(update={
        "metadata": metadata,
        "last_updated": datetime.now(tz=timezone.utc),
    })


class _KeepRef(Enum):
    """``_try_reconnect`` reused nothing, and the fresh sandbox must not
    be recorded over the ref: it names an older sandbox whose kill
    failed, which nothing else can reach."""

    KEEP_REF = "keep_ref"


# How long a session teardown, or a failed create, waits for its sandbox
# kill, cancels or not. One E2B API call, normally well under a second;
# the SDK's own request timeout is 60 s.
SANDBOX_KILL_TIMEOUT_SECONDS: float = 30.0

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Opt-in raw-stdout tracing for diagnosing the intermittent
# post-reattach discovery stall (some JSON-RPC responses stop arriving
# after waking a paused sandbox). When ``MCPOLIS_E2B_DEBUG_RAW_STDOUT=1``
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

# Cap on the error text logged when a process's output stream raises.
# SDK messages can carry a whole response body; the log line only
# needs the type and the first words of the reason.
_STREAM_ERROR_MAX_CHARS = 500
# How many links of the ``__cause__`` / ``__context__`` chain to name.
# ``_wrap_sdk_error`` keeps only the SDK class and message, and network
# errors often have an empty message, so the useful reason is usually
# one or two links down.
_STREAM_ERROR_CHAIN_DEPTH = 3


def _describe_one_error(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:
        message = ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _describe_stream_error(exc: BaseException) -> str:
    """One bounded line naming why a process's output stream raised,
    with up to two underlying causes joined by `` <- ``. Never raises:
    it runs inside the watcher's ``except``, ahead of the bookkeeping
    every stream death must complete."""
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < _STREAM_ERROR_CHAIN_DEPTH:
        parts.append(_describe_one_error(current))
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)[:_STREAM_ERROR_MAX_CHARS]


async def _clean_up_each(
    ids: Sequence[str],
    clean_up: Callable[[str], Awaitable[None]],
    *,
    id_field: str,
    events: tuple[str, str, str],
    org_id: str,
    upstream_id: str,
) -> list[str]:
    """Run ``clean_up`` once on each of ``ids``; return the ones it
    failed on, to retry later. One already gone (deleted in the E2B
    dashboard, an earlier partial teardown) counts as done. ``events``
    name the log lines: done, already gone, failed."""
    done_event, gone_event, failed_event = events
    left: list[str] = []
    for item_id in dict.fromkeys(ids):
        fields = {"org_id": org_id, "upstream_id": upstream_id, id_field: item_id}
        try:
            await clean_up(item_id)
            logger.info(done_event, **fields)
        except E2BNotFoundError:
            logger.info(gone_event, **fields)
        except E2BSDKError:
            logger.warning(failed_event, exc_info=True, **fields)
            left.append(item_id)
    return left


async def destroy_volumes(
    client: E2BClient,
    volume_ids: Sequence[str],
    *,
    org_id: str,
    upstream_id: str,
) -> list[str]:
    """Destroy each of ``volume_ids`` (a removed upstream's); return the
    ones whose destroy failed, to retry later."""
    return await _clean_up_each(
        volume_ids, client.destroy_volume,
        id_field="volume_id",
        events=(
            "sandbox.e2b.volume.destroyed",
            "sandbox.e2b.volume.destroy_not_found",
            "sandbox.e2b.volume.destroy_failed",
        ),
        org_id=org_id, upstream_id=upstream_id,
    )


async def kill_sandboxes(
    client: E2BClient,
    sandbox_ids: Sequence[str],
    *,
    org_id: str,
    upstream_id: str,
) -> list[str]:
    """Kill each of ``sandbox_ids`` by id, whatever instance tag it
    carries (a removed upstream's, see ``SANDBOXES_TO_KILL_METADATA_KEY``);
    return the ones whose kill failed, to retry later."""
    return await _clean_up_each(
        sandbox_ids, client.kill_sandbox,
        id_field="sandbox_id",
        events=(
            "sandbox.e2b.leftover_sandbox.killed",
            "sandbox.e2b.leftover_sandbox.already_gone",
            "sandbox.e2b.leftover_sandbox.kill_failed",
        ),
        org_id=org_id, upstream_id=upstream_id,
    )


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
        kill_timeout_seconds: float = SANDBOX_KILL_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._mcpolis_instance = mcpolis_instance
        self._kill_timeout_seconds = kill_timeout_seconds
        # Kills still running when their wait gave up: held until they
        # end, so Python cannot collect one halfway.
        self._overdue_kills = BackgroundTaskSet()
        # Set by the first ``_session_cm``. After that the instance id
        # is on sandboxes and refs, so it may no longer change.
        self._sessions_started = False
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
        # Set once by ``mark_all_active_sessions_preserve_on_close``
        # (the shutdown cleanup) and never cleared: every session that
        # closes afterwards is preserved, including one a connect still
        # in flight registers after the mark. Without it that late
        # session was killed and its ref deleted, where the old
        # SIGKILL-on-deploy left both alone.
        self._shutting_down = False
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
        self._sessions_started = True
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

        # Fatal-transport signal. Set when the stdin pump gives up:
        # the sandbox is gone, a send failed, or the sandbox woke from
        # a pause and its frozen process was retired. The connection
        # task surfaces this via ``is_transport_alive()`` so the
        # manager rebuilds the session instead of reusing the zombie.
        # A wake is no longer "transient" — it ends the session by
        # design, because the process on the other side cannot be
        # trusted after a freeze. See ``_fail_transport`` below.
        transport_failed = asyncio.Event()

        # E2B pauses the sandbox ``on_timeout_seconds`` after the last
        # ``set_timeout``, whatever the traffic. This re-arms it on MCP
        # traffic so the pause measures idle time. It records traffic
        # from here on; its refresh loop starts once the sandbox exists.
        pause_timer = IdlePauseTimer(
            idle_seconds=self._on_timeout_seconds, session_id=session_id,
        )

        async def _fail_transport() -> None:
            """Mark the transport dead and close the read side so the
            ``ClientSession`` read loop ends and fails every in-flight
            request with ``CONNECTION_CLOSED`` immediately — rather than
            each waiting out its full ``wait_for`` timeout. (Sending an
            ``Exception`` object down ``read_writer`` does NOT do this:
            MCP SDK ≥1.x routes it to the message handler, which drops
            it, leaving the pending request to hang.)

            Also stops the pause timer. The requests it was waiting on
            will never be answered on this transport, and keeping the
            sandbox awake for them would be a keep-alive.
            """
            transport_failed.set()
            pause_timer.stop()
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
                # Only answers can count, and the timer keeps only those
                # to a caller's request. Messages the server sends on its
                # own (logs, progress, answers to nothing) must not keep a
                # sandbox awake that nobody is using.
                if isinstance(
                    msg.root, (types.JSONRPCResponse, types.JSONRPCError),
                ):
                    pause_timer.response_received(msg.root.id)
                try:
                    await read_writer.send(SessionMessage(message=msg))
                except (
                    anyio.ClosedResourceError, anyio.BrokenResourceError,
                ):
                    # The read side is shut. Since the watcher fails the
                    # transport on EVERY session end, not just failures,
                    # a chatty server still flushing output at teardown
                    # lands here routinely. Swallow it: raising would
                    # surface as an uncaught exception inside the E2B
                    # SDK's own callback task, once per late chunk.
                    return
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
        # Acquire the (sandbox, process) pair: reuse a persisted
        # sandbox if reuse-on-restart is enabled, else fresh-create.
        # Either way the process is new and its pid is persisted below,
        # unless the ref must keep naming an older sandbox (``record``).
        sandbox, process, record = await self._acquire_session_handles(
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
        # its own persistence path on pause()). Always written: every
        # path yields a new pid, so a skipped write would leave the ref
        # pointing at a process that no longer exists. The one exception
        # (``record`` False): the ref names an older sandbox only it can
        # reach, whose kill failed (see ``_try_reconnect``). This session
        # then goes unrecorded: a reopen does not keep its sandbox
        # (``preserve_sessions_for_upstream`` goes by the recorded owner),
        # and one a shutdown keeps is an orphan the next boot reconciles.
        if (
            self._reuse_sandboxes_on_restart
            and resume_from is None
            and self._persistence is not None
            and record
        ):
            try:
                await self._persist_live_ref(
                    org_id=org_id,
                    upstream=upstream,
                    sandbox_id=sandbox.sandbox_id,
                    pid=process.pid,
                    # Records the size this sandbox was built with, so
                    # a later reuse refuses once the operator edits
                    # cpu/ram (a reconnect cannot re-size).
                    template=self._resolve_template(
                        upstream=upstream, resources=resources,
                    ),
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
        # the stdin pump retires the process and fails the transport
        # so the session is rebuilt against the same sandbox. It used
        # to reattach to the same pid here; see the long note at that
        # branch for why a process cannot outlive a freeze.
        stream_dead = asyncio.Event()

        async def watch_stream(p: E2BProcessHandle) -> None:
            exit_code: int | None = None
            # Distinguishes the two reasons this task ends. Without it
            # ``exit_code`` is null for both a pause and an ordinary
            # close, so the ``stream_dead`` rate chart is unreadable:
            # an operator cannot tell "sandboxes are pausing more" from
            # "sessions are being restarted more".
            cause = "severed"
            # Why the stream ended when it RAISED rather than returned.
            # Sentry MCPOLIS-BACKEND-1E: a fresh process on a woken
            # sandbox lost its stream 5 ms after start, and with this
            # swallowed nothing said why.
            stream_error: str | None = None
            try:
                exit_code = await p.wait()
            except asyncio.CancelledError:
                # Teardown cancelled us; the session is closing on
                # purpose. Re-raised below after the bookkeeping.
                cause = "closed"
                raise
            except BaseException as exc:
                # Any exception out of wait() also means the events
                # stream is no longer delivering — fold it into the
                # same dead-stream signal so the pump retires the
                # process and hands off to a session rebuild.
                # ``exit_code`` stays ``None``; the snapshot will
                # report "exited (code unknown)".
                stream_error = _describe_stream_error(exc)
            finally:
                # ``mark_exited`` is no-op after the first call, so
                # a later observation can't overwrite a real
                # subprocess-exit one captured during the init window.
                # There is only one watcher per session now: the pump
                # ends the session on a wake rather than re-pointing
                # at a new handle, so no second watcher is spawned.
                exit_signal.mark_exited(exit_code)
                stream_dead.set()
                logger.info(
                    "sandbox.e2b.stream_dead",
                    session_id=session_id,
                    exit_code=exit_code,
                    cause=cause,
                    stream_error=stream_error,
                )
                # Declare the transport dead HERE, not when something
                # next tries to write. E2B's pause severs the streaming
                # RPC, so this fires during the idle window: measured
                # 8.7s before the next request arrived. Spending that
                # head start is what makes the whole problem go away.
                #
                # ``ensure_shared_connected`` refuses a session whose
                # ``is_transport_alive()`` is false, and it runs inside
                # ``_resolve_session`` BEFORE the gateway writes
                # anything. So the next request finds a dead session,
                # gets a rebuilt one, and lands on a fresh MCP process.
                # No request is ever handed to a frozen process, which
                # means none is ever lost, which means nothing needs
                # re-sending.
                #
                # That is why there is no "may I retry?" machinery in
                # this file. An earlier design let the request reach a
                # dead session, then tried to prove after the fact that
                # re-sending was safe. Two rounds of adversarial review
                # each found a hole in that proof. This ordering has no
                # proof to get wrong.
                #
                # LIMIT, stated because an earlier comment here got it
                # backwards: if E2B ever goes QUIET instead of severing
                # the stream, this watcher never fires, and the pump's
                # branch below cannot cover for it — that branch is
                # gated on ``stream_dead``, which only this line sets.
                # The frame would then be written to a frozen process
                # with dead pooled sockets: Sentry MCPOLIS-BACKEND-16
                # again, silently. Severing is what E2B does today
                # (measured: the watcher fires 8.7s before the next
                # request), but nothing in our code enforces it. An
                # independent detector — a last-stdout age check, or an
                # unconditional respawn on every session open — is the
                # fix if that ever changes.
                await _fail_transport()

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
            # No ``nonlocal``: the pump no longer swaps ``process`` or
            # ``watch_task`` mid-session. A woken sandbox retires its
            # process and ends the session instead of re-pointing at a
            # new handle, so both are read-only closures here.
            try:
                async with write_reader:
                    async for session_message in write_reader:
                        if stream_dead.is_set():
                            # The sandbox auto-paused and this frame is
                            # the first traffic since. We do NOT reattach
                            # to the frozen MCP process.
                            #
                            # Reattaching used to happen right here, and
                            # it is what produced two user-visible bugs.
                            # A process resumed from a snapshot believes
                            # it still owns every TCP connection its HTTP
                            # client had pooled; those were severed while
                            # it slept. It writes into them and gets
                            # ECONNRESET, one failed request per pooled
                            # socket, which reaches the user as an opaque
                            # "Upstream tool call failed". Measured 20 out
                            # of 20 wakes, failure count tracking pool
                            # size exactly, in
                            # ``tests/integration/diagnose_wake_network.py``.
                            # The same reuse carries envd's fan-out wedge,
                            # which is the silent-stdout stall.
                            #
                            # Backstop, not the main path. The watcher
                            # normally declares the transport dead the
                            # moment the stream ends, so the gateway
                            # rebuilds before writing and no frame
                            # reaches here. This branch catches the one
                            # real race: a dispatch that passed the
                            # liveness gate microseconds before the
                            # watcher fired. It does NOT cover a
                            # watcher that never fires — see the note
                            # at ``watch_stream``.
                            #
                            # Either way the frame is NOT written: the
                            # dispatch fails and the manager rebuilds.
                            # The caller eats one error, which is the
                            # honest outcome when we cannot tell
                            # whether an earlier request on this
                            # session already ran.
                            kill_started = time.monotonic()
                            try:
                                await sandbox.kill_command(pid=process.pid)
                            except (
                                ConnectionError, OSError, E2BSDKError,
                            ) as exc:
                                # Non-fatal: the replacement session is
                                # what matters, and a resident process
                                # costs memory inside a sandbox we may
                                # be about to abandon anyway.
                                logger.warning(
                                    "sandbox.e2b.wake.kill_failed",
                                    session_id=session_id,
                                    pid=process.pid,
                                    error=str(exc),
                                )
                            logger.info(
                                "sandbox.e2b.wake.process_retired",
                                session_id=session_id,
                                pid=process.pid,
                                kill_duration_ms=round(
                                    (time.monotonic() - kill_started) * 1000, 1,
                                ),
                            )
                            await _fail_transport()
                            return
                        body = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True,
                        )
                        # Requests only (the timer keeps the callers'
                        # ones): our answers to the server's own requests,
                        # and notifications, are not a caller using the
                        # sandbox. Recorded BEFORE the write: a fast
                        # server answers while ``send_stdin``'s own HTTP
                        # call is still returning, and an answer that
                        # beats its request's record leaves the request
                        # "unanswered", keeping the sandbox awake until the
                        # cap. The real-SDK test caught exactly that.
                        root = session_message.message.root
                        if isinstance(root, types.JSONRPCRequest):
                            pause_timer.request_sent(root.id, root.method)
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
        pause_timer_task: asyncio.Task[None] = asyncio.create_task(
            pause_timer.run(
                lambda: sandbox.set_timeout(self._on_timeout_seconds),
            ),
        )

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
            # A shutdown keeps the sandbox for the next boot to reuse.
            # With reuse off nothing would ever look for it again, so it
            # is killed like on any other close.
            preserve = (
                self._preserve_on_close.pop(session_id, False)
                or (self._shutting_down and self._reuse_sandboxes_on_restart)
            )
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

            # Stop re-arming the pause timer before the kill below, so a
            # refresh never races a dying sandbox. A preserved sandbox
            # keeps its last deadline and pauses on its own.
            pause_timer_task.cancel()
            try:
                await pause_timer_task
            except (asyncio.CancelledError, Exception):
                pass

            # Skip the sandbox kill in two cases:
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
            # The streaming RPC is released either way, the kill first:
            # the underlying httpx generator needs a controlled close
            # (see the post-reattach release comment block in the
            # stdin pump for the GC-vs-__aexit__ race rationale).
            should_kill = not was_paused and not preserve
            try:
                if should_kill:
                    # ``close()`` cancels this task once CLOSE_TIMEOUT
                    # (10 s) has passed, and a wedged MCP, whose close
                    # is slow, is exactly what an admin stops. The kill
                    # is carried through that cancel, which then goes on.
                    await self._finish_despite_cancels(
                        self._kill_closed_session_sandbox(
                            session_id=session_id,
                            sandbox=sandbox,
                            process=process,
                            owner=owner,
                        ),
                        sandbox_id=sandbox.sandbox_id,
                    )
                else:
                    await self._release_process(process)
            finally:
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
        """Return ``(sandbox, process, record)`` for a session.

        Three paths:

        1. **Resume from snapshot** (caller passed ``resume_from``):
           ``connect_sandbox(snapshot_id)`` + fresh ``run_command``.
           This is the explicit-pause path — the previous session
           was paused, the snapshot is the resumed-from id. Always
           returns ``was_reconnect=False`` because the MCP process
           inside the snapshot is dropped and a new one starts.
        2. **Reuse-on-restart** (``_reuse_sandboxes_on_restart`` is
           on AND persistence has a live ref):
           ``connect_sandbox(sandbox_id)`` + ``kill_command`` on the
           recorded pid + a fresh ``run_command``. The sandbox is
           reused; the MCP process inside it never is. Falls through
           to (3) on any failure (sandbox died, network blip). Reuse
           is unconditional: a config edit on disk does NOT propagate
           until the user explicitly Stop+Restart the upstream.
        3. **Fresh create** (default): ``create_sandbox`` +
           ``run_command``. Returns ``was_reconnect=False``.

        Paths (1) and (2) are now the same shape, which is the point:
        a process that has been through a snapshot is never handed
        back to a caller. Path (2) still avoids most of the cold-start
        cost, because the expensive part of a fresh create is
        downloading the MCP's package (7-22 s in production) and that
        cache lives on the sandbox filesystem, which survives.

        Every path yields a new pid that must be persisted. ``record``
        is False only when (2) left the ref naming an older sandbox whose
        kill failed (``_KeepRef``): the fresh sandbox of (3) must not be
        recorded over it.
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
            return sandbox, process, True

        # Path 2: reuse-on-restart — try reconnect to a persisted
        # live ref. Only fires when the operator has opted in AND
        # persistence is wired AND a recoverable ref exists.
        record = True
        if (
            self._reuse_sandboxes_on_restart
            and self._persistence is not None
        ):
            reconnect = await self._try_reconnect(
                org_id=org_id,
                upstream=upstream,
                resources=resources,
                argv=argv,
                merged_env=merged_env,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                materialize_files=materialize_files,
            )
            if isinstance(reconnect, tuple):
                logger.info(
                    "sandbox.e2b.reconnect.ok",
                    session_id=session_id,
                    org_id=org_id,
                    upstream_id=upstream.id,
                    sandbox_id=reconnect[0].sandbox_id,
                    pid=reconnect[1].pid,
                )
                return reconnect[0], reconnect[1], True
            record = reconnect is not _KeepRef.KEEP_REF

        # Path 3: fresh create. The default flow.
        #
        # Nothing is persisted until the create succeeds (``session()``
        # then writes the live ref). A start cut short by a crash leaves
        # a sandbox no ref points at, which the next boot's reconcile
        # kills. A "creating" marker used to be written here to stop a
        # reconcile from killing the sandbox of a start in flight; the
        # reconcile only runs at boot, before any start, so every marker
        # it found was a dead process's and kept that process's sandbox
        # alive for good.
        created: E2BSandboxHandle | None = None
        try:
            sandbox = await self._open_sandbox(
                org_id=org_id, upstream=upstream, resources=resources,
                resume_from=None,
            )
            created = sandbox
            await self._materialize_files(
                sandbox=sandbox,
                upstream_id=upstream.id,
                materialize_files=materialize_files,
            )
            # docker-language sandboxes have the Docker engine installed
            # but no running daemon — start it now before the MCP command
            # runs. Resume (path 1) and reuse (path 2) skip this because
            # the frozen microVM state restores with dockerd already
            # running.
            #
            # NOTE: path 2's justification used to be "it reattaches to
            # an already-live process", which stopped being true when
            # the wake fix made it spawn a fresh ``docker run`` instead.
            # The remaining claim — dockerd survives the snapshot — is
            # the same one path 1 has always relied on, so this is not
            # new exposure, but it is now load-bearing in a second
            # place. Worth a real docker-template check if the flock /
            # socket hazard in CLAUDE.md ever resurfaces.
            cfg = upstream.stdio
            if cfg is not None and language_for_command(cfg.command) == "docker":
                await self._start_docker_daemon(sandbox)
            process = await sandbox.run_command(
                argv, env=merged_env,
                on_stdout=on_stdout, on_stderr=on_stderr,
            )
        except BaseException:
            if created is not None:
                # The sandbox exists but the MCP never started in it (a
                # file failed to copy, the docker daemon or the command
                # failed to start, or this start was cancelled). Nothing
                # else knows its id, so kill it here, through a cancel
                # too, or it runs, then sits paused, until the next
                # boot's reconcile.
                await self._finish_despite_cancels(
                    self._kill_stranded_sandbox(
                        created, upstream_id=upstream.id,
                    ),
                    sandbox_id=created.sandbox_id,
                )
            raise
        return sandbox, process, record

    async def _kill_stranded_sandbox(
        self, sandbox: E2BSandboxHandle, *, upstream_id: str,
    ) -> None:
        """Best effort: the caller is already failing, and a kill that
        fails too must not replace its error."""
        try:
            await sandbox.kill()
        except (E2BSDKError, OSError):
            logger.warning(
                "sandbox.e2b.stranded_kill_failed",
                upstream_id=upstream_id,
                sandbox_id=sandbox.sandbox_id,
                exc_info=True,
            )
        else:
            logger.info(
                "sandbox.e2b.stranded_killed",
                upstream_id=upstream_id,
                sandbox_id=sandbox.sandbox_id,
            )

    async def _kill_closed_session_sandbox(
        self,
        *,
        session_id: str,
        sandbox: E2BSandboxHandle,
        process: E2BProcessHandle,
        owner: tuple[str, str] | None,
    ) -> None:
        """Kill a closed session's sandbox, then forget its ref.

        The sandbox kill ends every process in it, so the MCP process is
        not killed on its own. That kill went through envd first, which
        is exactly what hangs (60 s SDK timeout) when an admin stops a
        wedged MCP, and the sandbox kill behind it was then cut short.

        The ref is deleted only once the kill went through. Deleted
        first, a kill that failed or was cut short left a sandbox nothing
        pointed at: Stop's second chance (``kill_persisted_session``)
        found no ref, and the sandbox paused and stayed. Kept, the ref
        lets that second chance retry the kill.
        """
        try:
            await sandbox.kill()
        except E2BNotFoundError:
            pass  # Already gone: the end state wanted.
        except (E2BSDKError, OSError):
            logger.warning(
                "sandbox.e2b.sandbox.kill_failed",
                session_id=session_id,
                sandbox_id=sandbox.sandbox_id,
                exc_info=True,
            )
            await self._release_process(process)
            return
        if owner is not None:
            owner_org_id, owner_upstream_id = owner
            await self._forget_ref(
                org_id=owner_org_id,
                upstream_id=owner_upstream_id,
                sandbox_id=sandbox.sandbox_id,
            )
        await self._release_process(process)

    async def _release_process(self, process: E2BProcessHandle) -> None:
        """Close the streaming RPC behind ``process`` without killing
        the process (see ``E2BProcessHandle.release``)."""
        try:
            await process.release()
        except E2BSDKError:
            logger.warning(
                "sandbox.e2b.process.release_failed", exc_info=True,
            )

    async def _forget_ref(
        self, *, org_id: str, upstream_id: str, sandbox_id: str,
    ) -> None:
        """Forget ``sandbox_id`` in the ref of ``(org, upstream)`` if the
        ref still points at it. A newer session may have written its own
        ref while this one was being killed; that ref must stay.

        The upstream's persistent volume is kept (``storage_only``): the
        whole ref used to go, so the next Start provisioned a new, empty
        volume and the old one was never destroyed, not even when the
        upstream was removed."""
        if self._persistence is None:
            return
        try:
            ref = await self._persistence.get(
                org_id=org_id, upstream_id=upstream_id,
            )
        except Exception:
            logger.warning(
                "sandbox.e2b.persistence.delete_on_kill_failed",
                org_id=org_id,
                upstream_id=upstream_id,
                exc_info=True,
            )
            return
        if ref is not None and ref.sandbox_id == sandbox_id:
            await self._keep_only_storage(
                ref, failure_event="sandbox.e2b.persistence.delete_on_kill_failed",
            )

    async def _keep_only_storage(
        self,
        ref: SandboxPersistedRef,
        *,
        failure_event: str,
        also_to_kill: Sequence[str] = (),
    ) -> bool:
        """Rewrite ``ref`` once its sandbox is gone, or listed in
        ``also_to_kill`` as one still to kill: only what outlives the
        sandbox stays (``storage_only``), or the ref goes when that is
        nothing. Returns whether the write went through; a failure is
        logged as ``failure_event``."""
        assert self._persistence is not None  # every caller read ``ref`` there
        kept = storage_only(ref, also_to_kill=also_to_kill)
        try:
            if kept is not None:
                await self._persistence.upsert(kept)
            else:
                await self._persistence.delete(
                    org_id=ref.org_id, upstream_id=ref.upstream_id,
                )
        except Exception:
            logger.warning(
                failure_event,
                org_id=ref.org_id,
                upstream_id=ref.upstream_id,
                exc_info=True,
            )
            return False
        return True

    async def _finish_despite_cancels(
        self, work: Coroutine[object, object, None], *, sandbox_id: str,
    ) -> None:
        """Await ``work``, a sandbox kill and what follows it, to its
        end even if this task is cancelled meanwhile; then re-raise the
        first cancel that arrived.

        A kill cut half-way leaves a sandbox running that nothing else
        knows about. ``work`` runs in a task of its own
        (``finish_despite_cancels``), which neither a native
        ``Task.cancel()`` (``close()`` giving up after ``CLOSE_TIMEOUT``,
        a shutdown) nor an anyio cancel scope reaches. Only this call
        holds it, so a shutdown that refuses new jobs still lets it run.

        Bounded by ``kill_timeout_seconds``, cancelled or not: a kill
        still running then is cancelled and left to unwind on its own
        (held by ``_overdue_kills``), so a hung E2B API cannot hold the
        caller forever. A failure is logged, never raised: the caller is
        already closing or failing.
        """

        def report_timeout() -> None:
            logger.warning(
                "sandbox.e2b.kill_timed_out",
                sandbox_id=sandbox_id,
                timeout_seconds=self._kill_timeout_seconds,
            )

        def report_failure(failure: BaseException) -> None:
            logger.warning(
                "sandbox.e2b.kill_cleanup_failed",
                sandbox_id=sandbox_id,
                exc_info=failure,
            )

        await finish_despite_cancels(
            work,
            held_by=None,
            time_limit=TimeLimit(
                self._kill_timeout_seconds,
                on_cut=report_timeout,
                cut_work_held_by=self._overdue_kills,
            ),
            on_failure=report_failure,
        )

    def _resolve_template(
        self,
        *,
        upstream: UpstreamDefinition,
        resources: SandboxResources,
    ) -> str:
        """The published template name for this upstream's language and
        requested size.

        One implementation for two callers that MUST agree: the create
        path, and the reuse check in :meth:`_try_reconnect`. Size is
        baked into the template at create time and a reconnect cannot
        re-size, so if these two ever disagreed a resized upstream
        would silently keep running at its old size.
        """
        cfg = upstream.stdio
        assert cfg is not None
        language = language_for_command(cfg.command)
        if language is None:
            raise ResourcesUnsupported(
                "cpu_vcpus",
                cfg.command,
                allowed=(
                    "npx", "uvx", "uv", "node", "python", "python3", "docker",
                ),
            )
        return self._grid.template_name(
            language=language,
            cpu_vcpus=resources.cpu_vcpus,
            memory_mb=resources.memory_mb,
        )

    async def _try_reconnect(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        resources: SandboxResources,
        argv: list[str],
        merged_env: dict[str, str],
        on_stdout: "Callable[[bytes], Awaitable[None] | None]",
        on_stderr: "Callable[[bytes], Awaitable[None] | None]",
        materialize_files: Sequence[MaterializeFile] | None = None,
    ) -> tuple[E2BSandboxHandle, E2BProcessHandle] | _KeepRef | None:
        """Reuse a persisted sandbox, but never its MCP process.

        Returns ``(sandbox, process)`` on success, ``None`` on any
        miss/failure (caller falls back to fresh create), and
        ``_KeepRef.KEEP_REF`` for the one miss whose ref the fresh
        create must not overwrite. Logs the specific reason for the
        miss so operators can diagnose unexpected fresh-creates.

        The sandbox is reconnected (keeping its warm filesystem and
        package cache, which is where nearly all of a cold start's
        7-22 s goes) but the MCP process recorded in the ref is KILLED
        and replaced with a fresh one. Reattaching to it instead — what
        this method used to do — hands back a process that was frozen
        mid-flight: its HTTP client's pooled sockets were severed
        while it slept, it cannot tell, and it writes into them. That
        surfaces to the user as one opaque failure per pooled socket
        on the first calls after a wake, measured 20 out of 20 wakes
        in ``tests/integration/diagnose_wake_network.py``, and is the
        same reuse that carries the envd fan-out wedge behind the
        silent-stdout stall.

        Because the pid changes, the caller re-persists the live ref
        (it already writes ``process.pid`` unconditionally).
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
        # Only a sandbox carrying the current instance tag is reused.
        # The boot reconcile lists sandboxes by that tag, and E2B fixes a
        # sandbox's metadata at create, so one carrying any other tag
        # (created before the tag became one value per database) stays
        # invisible to it: reused, it would leak for good the day its ref
        # goes. A ref that does not record the tag predates this check
        # and is not reused either.
        sandbox_instance = ref.metadata.get(SANDBOX_INSTANCE_METADATA_KEY)
        if sandbox_instance != self._mcpolis_instance:
            logger.info(
                "sandbox.e2b.reconnect.other_instance",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id,
                sandbox_instance=sandbox_instance,
                mcpolis_instance=self._mcpolis_instance,
                fallback="fresh_create",
            )
            # The fresh create overwrites this ref, after which nothing
            # points at that sandbox and no reconcile can see it.
            if await self._kill_stale_sandbox(sandbox_id=ref.sandbox_id):
                return None
            # The kill failed. Overwritten, the ref would lose that
            # sandbox for good (paused, on the account), so the ref keeps
            # naming it and the fresh sandbox goes unrecorded: the next
            # reopen, Stop or removal retries the kill from the ref. The
            # other misses below kill a sandbox carrying the current tag,
            # which the next boot's reconcile finds if its kill failed.
            logger.warning(
                "sandbox.e2b.reconnect.other_instance_kept",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id,
            )
            return _KeepRef.KEEP_REF
        # Sandbox reuse is unconditional: we reuse whatever sandbox is
        # alive regardless of whether the live config changed since it
        # was created. The old config-hash gate was removed because it
        # silently applied pending edits across deploys.
        #
        # DRIFT CAVEAT (changed by the wake fix, 2026-09): the
        # replacement process started below gets the CURRENT argv/env
        # and freshly materialized Sandbox files. While a wake was a
        # transparent reattach, an edit really could not take effect
        # without an explicit Stop+Restart. Now a wake ends the session
        # and the manager rebuilds through here, so a pending edit goes
        # live on the next wake and ``connect_shared`` re-persists
        # ``started_config_hash``, clearing the dirty-config banner on
        # its own. That is a behaviour change for the operator, not an
        # accident; see the CLAUDE.md wake section.
        # A sandbox's CPU and RAM come from the template it was
        # created with, and reconnecting attaches by id — it cannot
        # re-size. So a size edit has to fresh-create, or the upstream
        # silently keeps running at the old size while the dashboard
        # clears its dirty-config banner (``cpu_vcpus`` / ``memory_mb``
        # are in the runtime hash, and every reopen re-persists it).
        # Caught in review after the preserve-the-sandbox change made
        # reuse the norm; before that, reopens fresh-created anyway.
        wanted_template = self._resolve_template(
            upstream=upstream, resources=resources,
        )
        ref_template = ref.metadata.get("e2b_template")
        if ref_template is not None and ref_template != wanted_template:
            logger.info(
                "sandbox.e2b.reconnect.template_changed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id,
                from_template=ref_template,
                to_template=wanted_template,
                fallback="fresh_create",
            )
            # The fresh create overwrites this ref, after which nothing
            # points at the old-size sandbox: kill it now or it leaks.
            await self._kill_stale_sandbox(sandbox_id=ref.sandbox_id)
            return None

        # Try the actual reconnect.
        try:
            sandbox = await self._client.connect_sandbox(ref.sandbox_id)
        except E2BSDKError as exc:
            logger.info(
                "sandbox.e2b.reconnect.connect_sandbox_failed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, error=str(exc),
            )
            # Usually the sandbox is already gone (a no-op kill). If it
            # is only unreachable, the fresh create is about to
            # overwrite the ref that points at it, so kill it too.
            await self._kill_stale_sandbox(sandbox_id=ref.sandbox_id)
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
        # Kill the frozen MCP process before starting its replacement.
        # Best-effort: a pid that is already gone is the end state we
        # want, and a genuine failure costs one resident process
        # rather than the session. Skipping it entirely would leave
        # one dead MCP server per wake, and a busy sandbox wakes
        # dozens of times a day.
        try:
            await sandbox.kill_command(pid=ref.pid)
        except E2BSDKError as exc:
            logger.warning(
                "sandbox.e2b.reconnect.kill_stale_process_failed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, pid=ref.pid, error=str(exc),
            )
        # Sandbox files are re-materialized before the replacement
        # process starts, mirroring the snapshot-resume path: the
        # sandbox disk survived, but a Variable edited since the
        # sandbox was created must reach the new process.
        #
        # ``_materialize_files`` treats a write failure as fatal and
        # lets it propagate, which is right on the fresh-create path
        # (a sandbox that cannot take its credential files is no use)
        # but wrong here: this method's contract is to return ``None``
        # on a PROVIDER failure so the caller fresh-creates. An
        # unwrapped raise would abort the session open instead,
        # turning a stale sandbox into a user-visible error. A
        # ``ValueError`` from ``confine_to_sandbox_home`` is
        # deliberately NOT caught — a path escaping the sandbox home
        # is a config bug that a fresh create would hit identically,
        # so it should surface rather than loop.
        try:
            await self._materialize_files(
                sandbox=sandbox,
                upstream_id=upstream.id,
                materialize_files=materialize_files,
            )
        except (ConnectionError, OSError, E2BSDKError) as exc:
            logger.warning(
                "sandbox.e2b.reconnect.materialize_failed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, error=str(exc),
                fallback="fresh_create",
            )
            await self._kill_stale_sandbox(
                sandbox_id=ref.sandbox_id, sandbox=sandbox,
            )
            return None
        respawn_started = time.monotonic()
        try:
            process = await sandbox.run_command(
                argv, env=merged_env,
                on_stdout=on_stdout, on_stderr=on_stderr,
            )
        except E2BSDKError as exc:
            elapsed_ms = round(
                (time.monotonic() - respawn_started) * 1000.0, 1,
            )
            # Severity is ``warning`` (not ``info``) because this path
            # delays a user-visible Start click by the full respawn
            # attempt before falling back to a fresh create.
            # ``elapsed_ms`` and ``error_class`` let dashboards split
            # routine "sandbox auto-stopped" failures (fast, expected)
            # from "E2B API degraded" ones (slow, actionable) without
            # grepping the freeform ``error`` string.
            # ``fallback="fresh_create"`` makes the self-healing
            # handoff explicit so a single log line carries the full
            # story rather than implying it from the
            # ``sandbox.e2b.create`` event ~hundreds-of-ms later.
            logger.warning(
                "sandbox.e2b.reconnect.respawn_failed",
                org_id=org_id, upstream_id=upstream.id,
                sandbox_id=ref.sandbox_id, stale_pid=ref.pid,
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
    ) -> bool:
        """Best-effort kill of a stale sandbox during reconnect recovery.

        Used by :meth:`_try_reconnect` when the respawn fails: we
        could not start a process in this sandbox, so it is no use to
        us, and killing it avoids a leak before the caller's
        fresh-create path produces a replacement.

        Always best-effort — a failure here can't block the caller's
        fresh-create path. The reconciler is the eventual-consistency
        net for any sandbox that survives the kill attempt and carries
        the current instance tag. Returns whether the sandbox is gone
        (killed, or already gone).
        """
        try:
            if sandbox is not None:
                await sandbox.kill()
            else:
                await self._client.kill_sandbox(sandbox_id)
        except E2BNotFoundError:
            # Usual after a failed reconnect: the sandbox is gone.
            logger.info(
                "sandbox.e2b.reconnect.stale_already_gone",
                sandbox_id=sandbox_id,
            )
        except E2BSDKError:
            logger.warning(
                "sandbox.e2b.reconnect.stale_kill_failed",
                sandbox_id=sandbox_id, exc_info=True,
            )
            return False
        return True

    async def _persist_live_ref(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        sandbox_id: str,
        pid: int,
        template: str | None = None,
    ) -> None:
        """Write a live-ref to persistence so the next boot can
        reconnect via :meth:`_try_reconnect`.

        ``template`` records the size the sandbox was built with, so a
        later reuse can refuse when the operator has changed cpu/ram.
        ``None`` leaves whatever the row already had.

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
        if template is not None:
            merged_metadata["e2b_template"] = template
        # The sandbox carries the current tag: a fresh create tags it so,
        # and ``_try_reconnect`` reuses no other.
        merged_metadata[SANDBOX_INSTANCE_METADATA_KEY] = self._mcpolis_instance
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
        # where the image's systemd-managed dockerd is already up but
        # left the socket root-only. If the socket doesn't exist yet
        # this is a no-op.
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
        template = self._resolve_template(
            upstream=upstream, resources=resources,
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

        Also latches the service into shutdown: sessions that register
        after this call (a connect that was still in flight) are
        preserved too when they close. Only the shutdown cleanup calls
        it; the service is not used afterwards.

        With reuse on restart off it marks nothing: the next boot would
        never look for a kept sandbox, so each one is killed as it
        closes, like on any other close.
        """
        self._shutting_down = True
        if not self._reuse_sandboxes_on_restart:
            return 0
        for sid in list(self._live_sandboxes):
            self._preserve_on_close[sid] = True
        return len(self._live_sandboxes)

    def preserve_sessions_for_upstream(
        self, *, org_id: str, upstream_id: str,
    ) -> int:
        """See :meth:`SandboxService.preserve_sessions_for_upstream`.

        Scoped by ``_session_owners``, which the session flow fills
        whenever reuse-on-restart is wired — precisely the case where
        preserving is worth anything. Sessions belonging to other
        upstreams are untouched, so a reopen of one MCP cannot leak
        another's sandbox past its own teardown.

        Within ONE upstream this marks every live session, while a
        reopen closes only the shared one. A second live session would
        therefore carry a stale mark into an unrelated teardown.
        Unreachable today: ``validate_stdio_uses_service_account``
        forbids non-service-account stdio, so there is only ever one.
        Revisit if stdio ever gains per-user sessions.

        CONTRACT: the caller must CLOSE the session it marks. The mark
        is a latch with no expiry — marking and then leaving the
        session open means its eventual, unrelated teardown silently
        skips killing the sandbox. ``connect_shared`` satisfies this
        by closing immediately afterwards.
        """
        marked = 0
        for session_id, owner in list(self._session_owners.items()):
            if owner != (org_id, upstream_id):
                continue
            if session_id in self._live_sandboxes:
                self._preserve_on_close[session_id] = True
                marked += 1
        if marked:
            logger.info(
                "sandbox.e2b.preserve_for_heal",
                org_id=org_id, upstream_id=upstream_id, sessions=marked,
            )
        return marked

    def adopt_instance_id(self, instance_id: str) -> None:
        """Replace the provisional per-process instance id with the
        store's stable one (``get_or_create_instance_id``).

        The app is built synchronously, before the store can be read,
        so the lifespan calls this once at boot, before any MCP
        connects. Refuses once a session has started: that session's
        sandbox and ref already carry the old id, and the reconciler
        would treat it as another environment's.
        """
        if not instance_id:
            raise ValueError("instance_id must be non-empty")
        if self._sessions_started:
            raise RuntimeError(
                "cannot change the sandbox instance id after a session"
                " has started",
            )
        self._mcpolis_instance = instance_id

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
           ``Sandbox.kill(sandbox_id)`` (a sandbox already gone counts
           as killed).
        3. Clears the ref so the next ``_try_reconnect`` finds
           nothing and falls through to fresh-create. A persistent
           volume is not a sandbox: it stays on the ref
           (``storage_only``), for the fresh create to mount; deleting
           the ref used to lose the upstream's ``/data`` and leave its
           volume on the account for good. A sandbox whose kill failed
           stays on it too, as one still to kill
           (``SANDBOXES_TO_KILL_METADATA_KEY``), never to reuse: the boot
           reconcile right after retries the kill, whatever instance tag
           the sandbox carries. Cleared, the ref was the last thing that
           named a sandbox made with an older tag.

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
            not_killed = await _clean_up_each(
                [ref.sandbox_id] if ref.sandbox_id is not None else [],
                self._client.kill_sandbox,
                id_field="sandbox_id",
                events=(
                    "sandbox.e2b.fresh_restart.killed",
                    "sandbox.e2b.fresh_restart.already_gone",
                    "sandbox.e2b.fresh_restart.kill_failed",
                ),
                org_id=ref.org_id, upstream_id=ref.upstream_id,
            )
            if await self._keep_only_storage(
                ref,
                failure_event="sandbox.e2b.fresh_restart.persistence_delete_failed",
                also_to_kill=not_killed,
            ):
                cleared += 1
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
    ) -> bool:
        """Clean up what E2B holds for ``(org, upstream)`` when the
        operator deletes the upstream (or its org): kill the sandbox the
        ref still names (one a Stop could not kill), destroy the volume
        it mounts, and retry what an earlier removal of the same id
        could not clean up. Then delete the persistence ref, so the
        reconciler doesn't see a phantom mapping.

        What fails stays on the ref, which keeps nothing else: a sandbox
        as one still to kill (``SANDBOXES_TO_KILL_METADATA_KEY``), a
        volume as one still to destroy (``VOLUMES_TO_DESTROY_METADATA_KEY``),
        never as the sandbox to reuse or the volume to mount. The boot
        reconcile retries them, a sandbox whatever instance tag it
        carries, and an upstream added again under the same id gets a
        sandbox and a volume of its own. Dropped, the ref was the last
        thing that named such a sandbox, and one made before the instance
        id became one value per database stayed on the account for good.

        Returns whether the ref was kept that way, for the boot reconcile
        to finish. Idempotent: a missing persistence ref, nothing to clean
        up, or an :class:`E2BNotFoundError` from the SDK all resolve to
        "nothing to clean up."
        """
        if self._persistence is None:
            return False
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream_id,
        )
        if existing is None:
            return False
        sandboxes_left = await kill_sandboxes(
            self._client,
            [
                *([existing.sandbox_id] if existing.sandbox_id else []),
                *sandboxes_to_kill(existing),
            ],
            org_id=org_id, upstream_id=upstream_id,
        )
        mounted = existing.metadata.get(VOLUME_METADATA_KEY)
        volumes_left = await destroy_volumes(
            self._client,
            [*([mounted] if mounted else []), *volumes_to_destroy(existing)],
            org_id=org_id, upstream_id=upstream_id,
        )
        tombstone = without_sandbox(
            existing, leftovers(volumes=volumes_left, sandboxes=sandboxes_left),
        )
        try:
            if tombstone is not None:
                await self._persistence.upsert(tombstone)
            else:
                await self._persistence.delete(
                    org_id=org_id, upstream_id=upstream_id,
                )
        except Exception:
            logger.warning(
                "sandbox.e2b.persistence.delete_failed",
                org_id=org_id, upstream_id=upstream_id, exc_info=True,
            )
            return False
        return tombstone is not None

    async def kill_persisted_session(
        self, *, org_id: str, upstream_id: str,
    ) -> None:
        """Kill the live E2B sandbox for ``(org, upstream)`` and clear
        the session-scoped fields of the persistence ref.

        What outlives the sandbox is kept (``storage_only``): the
        persistent volume, so the next ``_session_cm`` fresh-create
        reattaches the operator's ``/data`` disk, and what is still to
        clean up. When the ref records none of it, it is deleted.

        A kill that fails keeps the ref as it is: a later Stop, the next
        boot's (``boot_skip_disabled``) or a removal retries it from
        there. Cleared, the sandbox waited for a boot reconcile, and one
        tagged with another instance (made before the tag became one
        value per database) was never seen again. A missing ref and a
        sandbox already gone (24h cap, manual delete, prior partial
        teardown) resolve to "no-op, proceed".
        """
        if self._persistence is None:
            return
        existing = await self._persistence.get(
            org_id=org_id, upstream_id=upstream_id,
        )
        if existing is None:
            return
        if await _clean_up_each(
            [existing.sandbox_id] if existing.sandbox_id else [],
            self._client.kill_sandbox,
            id_field="sandbox_id",
            events=(
                "sandbox.e2b.persisted_session.killed",
                "sandbox.e2b.persisted_session.kill_not_found",
                "sandbox.e2b.persisted_session.kill_failed",
            ),
            org_id=org_id, upstream_id=upstream_id,
        ):
            return
        # Clear sandbox / pid / paused-snapshot so the next
        # ``_try_reconnect`` finds nothing and falls through to
        # fresh-create.
        await self._keep_only_storage(
            existing,
            failure_event="sandbox.e2b.persisted_session.persistence_clear_failed",
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
    "SANDBOXES_TO_KILL_METADATA_KEY",
    "VOLUME_METADATA_KEY",
    "VOLUMES_TO_DESTROY_METADATA_KEY",
    "destroy_volumes",
    "kill_sandboxes",
    "leftovers",
    "sandboxes_to_kill",
    "storage_only",
    "volumes_to_destroy",
    "with_leftovers_gone",
    "without_sandbox",
]
