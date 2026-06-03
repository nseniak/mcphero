"""Hand-rolled mock implementing the :class:`E2BClient` Protocol.

Used by the contract suite + the E2B-specific tests as a stand-in
for the real SDK. The mock records every call so tests can assert
on the SDK calls the service made (template name, metadata,
on_timeout backstop, etc.).

Per project convention this is built via ``make_mock_e2b_client()``
rather than a pytest fixture — callers pass it explicitly into the
service constructor.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mcpolis.adapters.sandbox_e2b.client import (
    E2BClient,
    E2BNotFoundError,
    E2BProcessHandle,
    E2BSandboxHandle,
    E2BSandboxInfo,
)


@dataclass
class _RecordedCreate:
    template: str
    metadata: dict[str, str]
    timeout_seconds: int
    volume_mounts: dict[str, str] | None = None


@dataclass
class _RecordedVolumeCreate:
    name: str
    volume_id: str


@dataclass
class _RecordedVolumeDestroy:
    volume_id: str


@dataclass
class _RecordedConnect:
    snapshot_id: str


@dataclass
class _RecordedCommand:
    argv: list[str]
    env: dict[str, str]


@dataclass
class _RecordedConnectCommand:
    pid: int


@dataclass
class _RecordedSetTimeout:
    sandbox_id: str
    timeout_seconds: int


@dataclass
class _RecordedKill:
    sandbox_id: str


@dataclass
class _RecordedFileWrite:
    sandbox_id: str
    path: str
    contents: str
    mode: int


class MockE2BSandboxInfo:
    """Concrete impl of :class:`E2BSandboxInfo`."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        state: str,
        metadata: dict[str, str],
        created_at: datetime | None = None,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._state = state
        self._metadata = dict(metadata)
        self._created_at = created_at or datetime.now(tz=timezone.utc)

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def metadata(self) -> dict[str, str]:
        return self._metadata

    @property
    def created_at(self) -> datetime:
        return self._created_at


class MockE2BProcessHandle:
    """Mock E2BProcessHandle. Captures stdin via ``stdin_buffer``;
    ``wait()`` blocks until the test calls ``simulate_exit``."""

    def __init__(
        self, exit_code: int = 0, *, pid: int = 1234,
    ) -> None:
        self._stdin: list[bytes] = []
        self._exit_event = asyncio.Event()
        self._exit_code = exit_code
        self._killed = False
        self._pid = pid
        # When set, ``send_stdin`` raises this exception instead of
        # buffering. Lets tests reproduce the case where the
        # underlying sandbox has died (E2B kill timer, network blip)
        # and stdin fails after the session was opened.
        self.stdin_send_error: Exception | None = None

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def stdin_buffer(self) -> list[bytes]:
        return list(self._stdin)

    @property
    def killed(self) -> bool:
        return self._killed

    def simulate_exit(self, code: int = 0) -> None:
        """Mark the process as exited so any task awaiting
        ``wait()`` returns.

        Used by tests two ways:
        (a) genuine MCP process exit, and (b) simulating that the
        E2B-side streaming RPC died after auto-pause — the SDK
        treats both as ``wait()`` returning, and the service uses
        that signal to trigger a reattach via ``connect_command``."""
        self._exit_code = code
        self._exit_event.set()

    async def send_stdin(self, data: bytes) -> None:
        if self.stdin_send_error is not None:
            raise self.stdin_send_error
        self._stdin.append(data)

    async def wait(self) -> int:
        await self._exit_event.wait()
        return self._exit_code

    async def release(self) -> None:
        # Non-destructive in the real SDK; in the mock we just mark
        # the handle as exited so any downstream ``wait()`` resolves
        # cleanly. Doesn't touch ``killed`` because the underlying
        # process is supposed to keep running.
        self._exit_event.set()

    async def kill(self) -> None:
        self._killed = True
        self._exit_event.set()


class MockE2BSandboxHandle:
    """Mock E2BSandboxHandle.

    The constructor takes a writable list to share recordings with the
    enclosing :class:`MockE2BClient` (so the test can introspect calls
    without holding both references).
    """

    def __init__(
        self,
        *,
        sandbox_id: str,
        client: "MockE2BClient",
    ) -> None:
        self._sandbox_id = sandbox_id
        self._client = client
        self.last_process: MockE2BProcessHandle | None = None
        self.killed: bool = False
        self.paused_snapshot_id: str | None = None

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    async def run_command(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> E2BProcessHandle:
        self._client.commands.append(
            _RecordedCommand(argv=list(argv), env=dict(env)),
        )
        # Save callbacks so tests can simulate stdout/stderr emission.
        self._client.last_on_stdout = on_stdout
        self._client.last_on_stderr = on_stderr
        process = MockE2BProcessHandle()
        self.last_process = process
        return process

    async def connect_command(
        self,
        *,
        pid: int,
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> E2BProcessHandle:
        self._client.connect_commands.append(_RecordedConnectCommand(pid=pid))
        # Mirror run_command: re-capture callbacks so tests driving a
        # post-reattach stdout still feed through ``last_on_stdout``.
        self._client.last_on_stdout = on_stdout
        self._client.last_on_stderr = on_stderr
        process = MockE2BProcessHandle(pid=pid)
        self.last_process = process
        return process

    async def set_timeout(self, timeout_seconds: int) -> None:
        self._client.set_timeouts.append(
            _RecordedSetTimeout(
                sandbox_id=self._sandbox_id,
                timeout_seconds=timeout_seconds,
            ),
        )

    async def pause(self) -> str:
        snapshot_id = f"snap-{self._sandbox_id}"
        self.paused_snapshot_id = snapshot_id
        # Move the corresponding info entry to ``paused`` state so a
        # subsequent ``list_sandboxes`` reflects reality.
        for info in self._client.live_infos:
            if info.sandbox_id == self._sandbox_id:
                self._client.live_infos.remove(info)
                self._client.live_infos.append(
                    MockE2BSandboxInfo(
                        sandbox_id=self._sandbox_id,
                        state="paused",
                        metadata=info.metadata,
                    ),
                )
                break
        return snapshot_id

    async def kill(self) -> None:
        self.killed = True
        self._client.kills.append(_RecordedKill(sandbox_id=self._sandbox_id))

    async def write_file(
        self,
        *,
        path: str,
        contents: str,
        mode: int = 0o600,
    ) -> None:
        self._client.file_writes.append(
            _RecordedFileWrite(
                sandbox_id=self._sandbox_id,
                path=path,
                contents=contents,
                mode=mode,
            ),
        )


@dataclass
class MockE2BClient(E2BClient):
    """In-memory mock matching :class:`E2BClient` for tests."""

    creates: list[_RecordedCreate] = field(default_factory=list[_RecordedCreate])
    connects: list[_RecordedConnect] = field(default_factory=list[_RecordedConnect])
    commands: list[_RecordedCommand] = field(default_factory=list[_RecordedCommand])
    connect_commands: list[_RecordedConnectCommand] = field(
        default_factory=list[_RecordedConnectCommand],
    )
    set_timeouts: list[_RecordedSetTimeout] = field(
        default_factory=list[_RecordedSetTimeout],
    )
    kills: list[_RecordedKill] = field(default_factory=list[_RecordedKill])
    file_writes: list[_RecordedFileWrite] = field(
        default_factory=list[_RecordedFileWrite],
    )
    deleted_snapshots: list[str] = field(default_factory=list[str])
    volume_creates: list[_RecordedVolumeCreate] = field(
        default_factory=list[_RecordedVolumeCreate],
    )
    volume_destroys: list[_RecordedVolumeDestroy] = field(
        default_factory=list[_RecordedVolumeDestroy],
    )
    # Force-error switches for the volume API (mirroring the existing
    # create_raises / connect_raises pattern further down). When set,
    # the matching method raises this exception instead of completing.
    volume_create_raises: Exception | None = None
    volume_destroy_raises: Exception | None = None
    # Sandboxes the mock pretends exist on the provider side.
    live_infos: list[MockE2BSandboxInfo] = field(
        default_factory=list[MockE2BSandboxInfo],
    )
    # Last stdout / stderr callbacks captured by run_command — tests
    # use these to simulate provider-side I/O.
    last_on_stdout: Callable[[bytes], Awaitable[None] | None] | None = None
    last_on_stderr: Callable[[bytes], Awaitable[None] | None] | None = None
    # Auto-incrementing id counter so each create gets a unique id.
    _next_id: int = 0
    # Map of snapshot_id → original metadata so connect_sandbox can
    # restore attribution (mimics how E2B carries metadata across pause).
    _snapshot_metadata: dict[str, dict[str, str]] = field(
        default_factory=dict[str, dict[str, str]],
    )
    # Force-error switches for tests.
    create_raises: Exception | None = None
    connect_raises: Exception | None = None

    async def create_sandbox(
        self,
        *,
        template: str,
        metadata: dict[str, str],
        timeout_seconds: int,
        volume_mounts: dict[str, str] | None = None,
    ) -> E2BSandboxHandle:
        if self.create_raises is not None:
            raise self.create_raises
        self.creates.append(
            _RecordedCreate(
                template=template,
                metadata=dict(metadata),
                timeout_seconds=timeout_seconds,
                volume_mounts=dict(volume_mounts) if volume_mounts else None,
            ),
        )
        sid = f"sbx-{self._next_id}"
        self._next_id += 1
        info = MockE2BSandboxInfo(
            sandbox_id=sid, state="running", metadata=dict(metadata),
        )
        self.live_infos.append(info)
        # Map a synthetic future-snapshot id back to the metadata so
        # connect_sandbox can resume cleanly without losing tags.
        self._snapshot_metadata[f"snap-{sid}"] = dict(metadata)
        return MockE2BSandboxHandle(sandbox_id=sid, client=self)

    async def connect_sandbox(self, snapshot_id: str) -> E2BSandboxHandle:
        if self.connect_raises is not None:
            raise self.connect_raises
        # Two acceptable inputs (mirrors the real E2B v2 API where
        # the snapshot id IS the sandbox id):
        #
        # - **Live sandbox id** (e.g. ``sbx-0``): reuse-on-restart
        #   path. The sandbox is still tracked in ``live_infos`` —
        #   we return a handle pointing to the SAME id, and the
        #   sandbox stays where it was.
        # - **Synthetic snapshot id** (e.g. ``snap-sbx-0``): legacy
        #   pause/resume path. Returns a NEW sandbox id, marks
        #   live_infos with running state for the resumed copy.
        live_match = next(
            (i for i in self.live_infos if i.sandbox_id == snapshot_id),
            None,
        )
        if live_match is not None:
            self.connects.append(_RecordedConnect(snapshot_id=snapshot_id))
            # If the sandbox was paused (auto-pause from on_timeout),
            # auto_resume flips it back to running. Keep the same id.
            if live_match.state != "running":
                self.live_infos.remove(live_match)
                self.live_infos.append(
                    MockE2BSandboxInfo(
                        sandbox_id=live_match.sandbox_id,
                        state="running",
                        metadata=live_match.metadata,
                    ),
                )
            return MockE2BSandboxHandle(
                sandbox_id=live_match.sandbox_id, client=self,
            )

        if snapshot_id not in self._snapshot_metadata:
            raise E2BNotFoundError(
                "E2BNotFoundError", f"snapshot {snapshot_id!r} not found",
            )
        self.connects.append(_RecordedConnect(snapshot_id=snapshot_id))
        # Snapshot-resume path: NEW sandbox id, original snapshot
        # cleared (resume consumes it).
        sid = f"sbx-{self._next_id}"
        self._next_id += 1
        meta = self._snapshot_metadata[snapshot_id]
        self.live_infos.append(
            MockE2BSandboxInfo(
                sandbox_id=sid, state="running", metadata=meta,
            ),
        )
        return MockE2BSandboxHandle(sandbox_id=sid, client=self)

    async def list_sandboxes(
        self, *, metadata_filter: dict[str, str] | None = None,
    ) -> list[E2BSandboxInfo]:
        if metadata_filter is None:
            return [info for info in self.live_infos]
        out: list[E2BSandboxInfo] = []
        for info in self.live_infos:
            if all(info.metadata.get(k) == v for k, v in metadata_filter.items()):
                out.append(info)
        return out

    async def kill_sandbox(self, sandbox_id: str) -> None:
        self.kills.append(_RecordedKill(sandbox_id=sandbox_id))
        self.live_infos = [
            info for info in self.live_infos
            if info.sandbox_id != sandbox_id
        ]

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted_snapshots.append(snapshot_id)
        self._snapshot_metadata.pop(snapshot_id, None)

    async def create_volume(self, *, name: str) -> str:
        if self.volume_create_raises is not None:
            raise self.volume_create_raises
        # Auto-incrementing ids mirror the real SDK's opaque-id
        # contract; tests don't read the format.
        volume_id = f"vol-{len(self.volume_creates)}"
        self.volume_creates.append(
            _RecordedVolumeCreate(name=name, volume_id=volume_id),
        )
        return volume_id

    async def destroy_volume(self, volume_id: str) -> None:
        if self.volume_destroy_raises is not None:
            raise self.volume_destroy_raises
        self.volume_destroys.append(
            _RecordedVolumeDestroy(volume_id=volume_id),
        )


def make_mock_e2b_client() -> MockE2BClient:
    """Project-convention builder."""
    return MockE2BClient()


__all__ = [
    "MockE2BClient",
    "MockE2BProcessHandle",
    "MockE2BSandboxHandle",
    "MockE2BSandboxInfo",
    "make_mock_e2b_client",
]
