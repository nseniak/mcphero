"""``RealE2BClient`` — real-SDK adapter tests.

Covers the boundary between the SDK and our :class:`E2BClient`
Protocol: lazy import, exception mapping, callback bridging
(bytes ↔ str), ``pause()`` returning the sandbox id, and the
SandboxInfo ``state`` translation.

These tests use ``unittest.mock.patch`` to stand in for the SDK
classes — no real E2B HTTP calls are made. CI-gated real-SDK
smoke tests against ``E2B_API_KEY`` live in
``test_e2b_sandbox_service_real_sdk.py`` (a future commit gates
this on the secret).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcpolis.adapters.sandbox_e2b import (
    E2BAuthError,
    E2BNotFoundError,
    E2BQuotaError,
    E2BSDKError,
    RealE2BClient,
    make_real_e2b_client,
)
from mcpolis.adapters.sandbox_e2b.real_client import (
    _RealCommandHandle,
    _RealSandboxHandle,
    _RealSandboxInfo,
    _wrap_sdk_error,
)


# ---------- constructor + factory ----------


def test_constructor_rejects_empty_api_key() -> None:
    """A blank api key is a config bug; refuse to construct so the
    error surfaces at startup rather than on the first session."""
    with pytest.raises(ValueError):
        RealE2BClient(api_key="")


def test_make_real_e2b_client_returns_client() -> None:
    client = make_real_e2b_client(api_key="e2b_test_xyz")
    assert isinstance(client, RealE2BClient)


# ---------- exception mapping ----------


def test_wrap_sdk_error_authentication() -> None:
    import e2b

    exc = e2b.AuthenticationException("bad key")
    wrapped = _wrap_sdk_error(exc)
    assert isinstance(wrapped, E2BAuthError)
    assert wrapped.error_class == "AuthenticationException"
    assert "bad key" in wrapped.detail


def test_wrap_sdk_error_rate_limit() -> None:
    import e2b

    exc = e2b.RateLimitException("429")
    wrapped = _wrap_sdk_error(exc)
    assert isinstance(wrapped, E2BQuotaError)


def test_wrap_sdk_error_not_found() -> None:
    import e2b

    exc = e2b.NotFoundException("missing")
    wrapped = _wrap_sdk_error(exc)
    assert isinstance(wrapped, E2BNotFoundError)


def test_wrap_sdk_error_unknown_falls_back_to_generic() -> None:
    """An unknown SDK exception class becomes a generic E2BSDKError —
    the service's map_exit then surfaces it as PROVIDER_ERROR with
    the raw class name + message in the detail string."""

    class WeirdException(Exception):
        pass

    wrapped = _wrap_sdk_error(WeirdException("exotic"))
    assert isinstance(wrapped, E2BSDKError)
    assert wrapped.error_class == "WeirdException"
    assert "exotic" in wrapped.detail


# ---------- _RealSandboxInfo ----------


def make_raw_info(
    *,
    sandbox_id: str = "sbx-1",
    state: str = "running",
    metadata: dict[str, str] | None = None,
    started_at: datetime | None = None,
) -> Any:
    raw = MagicMock()
    raw.sandbox_id = sandbox_id
    raw.metadata = metadata or {}
    raw.started_at = started_at
    if state:
        raw.state = MagicMock(value=state)
    else:
        raw.state = None
    return raw


def test_sandbox_info_state_translates_enum_to_string() -> None:
    raw = make_raw_info(state="paused")
    info = _RealSandboxInfo(raw)
    assert info.state == "paused"


def test_sandbox_info_handles_missing_state() -> None:
    raw = make_raw_info(state="")
    info = _RealSandboxInfo(raw)
    assert info.state == ""


def test_sandbox_info_metadata_coerces_non_string_values() -> None:
    raw = make_raw_info(metadata={"mcpolis_org": "acme", "count": 42})  # type: ignore[dict-item]
    info = _RealSandboxInfo(raw)
    assert info.metadata == {"mcpolis_org": "acme", "count": "42"}


def test_sandbox_info_falls_back_to_epoch_when_started_at_missing() -> None:
    raw = make_raw_info(started_at=None)
    info = _RealSandboxInfo(raw)
    # Reconciler treats epoch-zero as "old enough to GC if past
    # threshold" — explicit choice, not an accident.
    assert info.created_at.year == 1970


def test_sandbox_info_preserves_real_started_at() -> None:
    ts = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    raw = make_raw_info(started_at=ts)
    info = _RealSandboxInfo(raw)
    assert info.created_at == ts


# ---------- _RealSandboxHandle ----------


@pytest.mark.asyncio
async def test_pause_returns_sandbox_id() -> None:
    """v2 SDK: pause() returns None on the wire; the snapshot id is
    the same as the sandbox id (reconnect is by id). The adapter
    returns ``sandbox_id`` so SnapshotRef.snapshot_id round-trips
    cleanly through ``connect_sandbox``."""
    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-real-7"
    sdk_sandbox.pause = AsyncMock(return_value=None)

    handle = _RealSandboxHandle(sdk_sandbox)
    snapshot_id = await handle.pause()
    assert snapshot_id == "sbx-real-7"
    sdk_sandbox.pause.assert_awaited_once()


@pytest.mark.asyncio
async def test_pause_propagates_sdk_failure_as_typed_error() -> None:
    import e2b

    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-1"
    sdk_sandbox.pause = AsyncMock(
        side_effect=e2b.AuthenticationException("expired"),
    )
    handle = _RealSandboxHandle(sdk_sandbox)
    with pytest.raises(E2BAuthError):
        await handle.pause()


@pytest.mark.asyncio
async def test_kill_propagates_sdk_failure() -> None:
    sdk_sandbox = MagicMock()
    sdk_sandbox.kill = AsyncMock(side_effect=RuntimeError("boom"))
    handle = _RealSandboxHandle(sdk_sandbox)
    with pytest.raises(E2BSDKError):
        await handle.kill()


@pytest.mark.asyncio
async def test_run_command_bridges_byte_callbacks_to_str() -> None:
    """The adapter receives bytes-typed on_stdout / on_stderr from
    our service and feeds the SDK's str-typed callbacks. Round-trip
    a known byte sequence through to confirm the bridge holds."""
    captured_stdout: list[bytes] = []
    captured_stderr: list[bytes] = []

    async def on_stdout(b: bytes) -> None:
        captured_stdout.append(b)

    async def on_stderr(b: bytes) -> None:
        captured_stderr.append(b)

    sdk_sandbox = MagicMock()
    sdk_sandbox.commands = MagicMock()
    sdk_handle = MagicMock()
    sdk_handle.pid = 42

    captured_kwargs: dict[str, Any] = {}

    async def fake_run(cmd: str, **kwargs: Any) -> Any:
        captured_kwargs["cmd"] = cmd
        captured_kwargs.update(kwargs)
        # Simulate the SDK firing the callbacks once each.
        await kwargs["on_stdout"]("hello-stdout")
        await kwargs["on_stderr"]("hello-stderr")
        return sdk_handle

    sdk_sandbox.commands.run = fake_run

    handle = _RealSandboxHandle(sdk_sandbox)
    process = await handle.run_command(
        ["npx", "-y", "@modelcontextprotocol/server-everything"],
        env={"FOO": "bar"},
        on_stdout=on_stdout,
        on_stderr=on_stderr,
    )
    assert process is not None
    # SDK got a single string command.
    assert captured_kwargs["cmd"] == (
        "npx -y @modelcontextprotocol/server-everything"
    )
    assert captured_kwargs["envs"] == {"FOO": "bar"}
    assert captured_kwargs["background"] is True
    # Our bytes callbacks received the bridged data.
    assert captured_stdout == [b"hello-stdout"]
    assert captured_stderr == [b"hello-stderr"]


@pytest.mark.asyncio
async def test_connect_command_caps_a_wedged_establish() -> None:
    """A reattach whose envd port never opens ("sandbox is running but
    port is not open") must fail within the configured cap and surface
    a typed TimeoutException — not hang ~60s — so the reconnect path can
    fall back to a fresh create promptly. The SDK's request_timeout
    doesn't bound this (#1128), so the adapter wraps it in wait_for.
    """
    sdk_sandbox = MagicMock()
    sdk_sandbox.commands = MagicMock()

    async def _never_establishes(**kwargs: Any) -> Any:
        await asyncio.sleep(60)  # wedged envd port: start event never arrives

    sdk_sandbox.commands.connect = _never_establishes
    handle = _RealSandboxHandle(sdk_sandbox)

    async def _noop(_b: bytes) -> None:
        return None

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client"
        ".COMMANDS_CONNECT_REQUEST_TIMEOUT_SECONDS",
        0.05,
    ):
        with pytest.raises(E2BSDKError) as ei:
            await handle.connect_command(
                pid=1278, on_stdout=_noop, on_stderr=_noop,
            )
    assert ei.value.error_class == "TimeoutException"


@pytest.mark.asyncio
async def test_write_file_quotes_path_with_spaces_for_chmod() -> None:
    """The chmod that fixes up the mode runs as a shell command, so a
    target path with spaces (e.g. a macOS screenshot name reached via
    ``${HOME}/${FILE_NAME}``) must be shell-quoted. Unquoted, the
    shell word-splits it and chmod fails with "No such file or
    directory" on each fragment. The ``files.write`` call goes through
    the SDK (not a shell), so it receives the raw path verbatim.
    """
    sdk_sandbox = MagicMock()
    sdk_sandbox.files = MagicMock()
    sdk_sandbox.files.write = AsyncMock()
    sdk_sandbox.commands = MagicMock()
    sdk_sandbox.commands.run = AsyncMock()

    handle = _RealSandboxHandle(sdk_sandbox)
    spaced_path = "/home/user/Screenshot 2026-05-06 at 10.51.07.png"
    await handle.write_file(path=spaced_path, contents="data", mode=0o600)

    # SDK file write gets the raw (unquoted) path — it's not a shell.
    sdk_sandbox.files.write.assert_awaited_once_with(spaced_path, "data")
    # chmod runs the quoted path as a single shell token.
    run_args = sdk_sandbox.commands.run.await_args
    assert run_args is not None
    assert run_args.args[0] == (
        "chmod 600 '/home/user/Screenshot 2026-05-06 at 10.51.07.png'"
    )


@pytest.mark.asyncio
async def test_write_file_neutralizes_shell_metacharacters() -> None:
    """The target path is operator-controlled, so the chmod string is
    a shell-injection surface. Quoting must neutralize metacharacters
    — a path that would otherwise run ``rm`` stays a single inert
    argument to chmod."""
    sdk_sandbox = MagicMock()
    sdk_sandbox.files = MagicMock()
    sdk_sandbox.files.write = AsyncMock()
    sdk_sandbox.commands = MagicMock()
    sdk_sandbox.commands.run = AsyncMock()

    handle = _RealSandboxHandle(sdk_sandbox)
    evil_path = "/home/user/a; rm -rf ~ #"
    await handle.write_file(path=evil_path, contents="x", mode=0o600)

    run_args = sdk_sandbox.commands.run.await_args
    assert run_args is not None
    # The whole evil path is wrapped in one quoted token; the ``;``
    # and ``#`` lose their shell meaning.
    assert run_args.args[0] == "chmod 600 '/home/user/a; rm -rf ~ #'"


# ---------- _RealCommandHandle ----------


@pytest.mark.asyncio
async def test_command_send_stdin_routes_through_sandbox() -> None:
    """SDK exposes send_stdin on commands, not on the handle. The
    adapter holds a sandbox reference + the handle's pid to bridge."""
    sdk_sandbox = MagicMock()
    sdk_sandbox.commands = MagicMock()
    sdk_sandbox.commands.send_stdin = AsyncMock()
    sdk_handle = MagicMock()
    sdk_handle.pid = 17

    handle = _RealCommandHandle(sdk_sandbox, sdk_handle)
    await handle.send_stdin(b"jsonrpc-line\n")

    sdk_sandbox.commands.send_stdin.assert_awaited_once()
    args = sdk_sandbox.commands.send_stdin.await_args
    assert args is not None
    assert args.kwargs["pid"] == 17
    assert args.kwargs["data"] == "jsonrpc-line\n"


@pytest.mark.asyncio
async def test_command_wait_returns_int_exit_code() -> None:
    sdk_sandbox = MagicMock()
    sdk_handle = MagicMock()
    result_obj = MagicMock(exit_code=42)
    sdk_handle.wait = AsyncMock(return_value=result_obj)

    handle = _RealCommandHandle(sdk_sandbox, sdk_handle)
    code = await handle.wait()
    assert code == 42


@pytest.mark.asyncio
async def test_command_wait_handles_missing_exit_code() -> None:
    sdk_sandbox = MagicMock()
    sdk_handle = MagicMock()
    result_obj = MagicMock(exit_code=None)
    sdk_handle.wait = AsyncMock(return_value=result_obj)

    handle = _RealCommandHandle(sdk_sandbox, sdk_handle)
    code = await handle.wait()
    assert code == 0


# ---------- RealE2BClient delegation ----------


@pytest.mark.asyncio
async def test_create_sandbox_passes_template_metadata_timeout() -> None:
    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-new"

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncSandbox.create = AsyncMock(return_value=sdk_sandbox)
        # AuthenticationException etc. need to remain real classes
        # because _wrap_sdk_error checks isinstance against them.
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key-abc")
        await client.create_sandbox(
            template="mcpolis-node-cpu2-ram4096",
            metadata={"mcpolis_org": "acme"},
            timeout_seconds=3600,
        )

    e2b_mod.AsyncSandbox.create.assert_awaited_once()
    args = e2b_mod.AsyncSandbox.create.await_args
    assert args is not None
    call_kwargs = args.kwargs
    assert call_kwargs["template"] == "mcpolis-node-cpu2-ram4096"
    assert call_kwargs["timeout"] == 3600
    assert call_kwargs["metadata"] == {"mcpolis_org": "acme"}
    assert call_kwargs["api_key"] == "key-abc"
    # lifecycle must request pause-on-timeout + auto-resume so a
    # sandbox that goes idle past ``timeout`` is preserved as a
    # snapshot and transparently woken on the next API call,
    # rather than killed (the SDK default).
    assert call_kwargs["lifecycle"] == {
        "on_timeout": "pause",
        "auto_resume": True,
    }


@pytest.mark.asyncio
async def test_connect_sandbox_passes_snapshot_id() -> None:
    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-resumed"
    # ``connect_sandbox`` now probes envd readiness post-connect via
    # ``sandbox.is_running()`` to absorb the gap between gateway-side
    # auto_resume and envd binding its port. The probe must return
    # ``True`` here so the unit test exercises the happy path.
    sdk_sandbox.is_running = AsyncMock(return_value=True)

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncSandbox.connect = AsyncMock(return_value=sdk_sandbox)
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key-abc")
        await client.connect_sandbox("sbx-paused-from-yesterday")

    e2b_mod.AsyncSandbox.connect.assert_awaited_once()
    args = e2b_mod.AsyncSandbox.connect.await_args
    assert args is not None
    call_kwargs = args.kwargs
    assert call_kwargs["sandbox_id"] == "sbx-paused-from-yesterday"
    assert call_kwargs["api_key"] == "key-abc"
    # Envd-readiness probe was actually fired before returning a
    # handle — pins the post-connect wait that absorbs the
    # auto_resume → envd-bound gap.
    sdk_sandbox.is_running.assert_awaited()


@pytest.mark.asyncio
async def test_connect_sandbox_polls_envd_until_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Sandbox.connect`` returns the moment the gateway has woken
    a paused box, but envd inside takes a beat to rebind. The adapter
    must keep polling ``is_running()`` until envd is up — without
    that, the next envd-bound call (``commands.connect``) hits a 502
    that the SDK then turns into a confusing ``TimeoutException``.
    """
    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-slow-envd"
    # First two probes return False (envd still binding), third
    # returns True. The adapter must keep going.
    sdk_sandbox.is_running = AsyncMock(side_effect=[False, False, True])

    # Drop the poll interval so the test doesn't actually wait.
    from mcpolis.adapters.sandbox_e2b import real_client as rc_module
    monkeypatch.setattr(rc_module, "ENVD_READY_POLL_INTERVAL_SECONDS", 0.0)

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncSandbox.connect = AsyncMock(return_value=sdk_sandbox)
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key")
        await client.connect_sandbox("sbx-slow-envd")

    assert sdk_sandbox.is_running.await_count == 3, (
        f"expected 3 polls (False, False, True); got "
        f"{sdk_sandbox.is_running.await_count}"
    )


@pytest.mark.asyncio
async def test_connect_sandbox_raises_when_envd_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If envd doesn't come up within the deadline, the adapter must
    raise a typed error so ``_try_reconnect`` can fall back to a
    fresh-create instead of hanging the caller for an unbounded time.
    """
    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-stuck-envd"
    sdk_sandbox.is_running = AsyncMock(return_value=False)

    from mcpolis.adapters.sandbox_e2b import real_client as rc_module
    monkeypatch.setattr(rc_module, "ENVD_READY_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(rc_module, "ENVD_READY_TIMEOUT_SECONDS", 0.05)

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncSandbox.connect = AsyncMock(return_value=sdk_sandbox)
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key")
        with pytest.raises(E2BSDKError) as exc:
            await client.connect_sandbox("sbx-stuck-envd")
    assert "envd not reachable" in exc.value.detail


@pytest.mark.asyncio
async def test_create_sandbox_propagates_auth_failure() -> None:
    import e2b as real_e2b

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncSandbox.create = AsyncMock(
            side_effect=real_e2b.AuthenticationException("invalid key"),
        )
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key-abc")
        with pytest.raises(E2BAuthError) as exc:
            await client.create_sandbox(
                template="mcpolis-node-cpu1-ram1024",
                metadata={},
                timeout_seconds=60,
            )
    assert "invalid key" in exc.value.detail


# ---------- volume API ----------


@pytest.mark.asyncio
async def test_create_sandbox_passes_volume_mounts_when_present() -> None:
    """When the service supplies volume_mounts, the SDK's
    ``AsyncSandbox.create`` receives them verbatim. When omitted /
    empty, the kwarg is NOT included in the call (cleaner SDK logs
    + avoids exercising an SDK code path that may behave differently
    on edge inputs).
    """
    sdk_sandbox = MagicMock()
    sdk_sandbox.sandbox_id = "sbx-with-vol"

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncSandbox.create = AsyncMock(return_value=sdk_sandbox)
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key-abc")
        # With volume_mounts.
        await client.create_sandbox(
            template="mcpolis-node-cpu2-ram4096",
            metadata={},
            timeout_seconds=3600,
            volume_mounts={"/data": "vol-abc"},
        )
        args = e2b_mod.AsyncSandbox.create.await_args
        assert args is not None
        assert args.kwargs["volume_mounts"] == {"/data": "vol-abc"}

        # Without volume_mounts — kwarg must be absent.
        e2b_mod.AsyncSandbox.create.reset_mock()
        await client.create_sandbox(
            template="mcpolis-node-cpu2-ram4096",
            metadata={},
            timeout_seconds=3600,
        )
        args = e2b_mod.AsyncSandbox.create.await_args
        assert args is not None
        assert "volume_mounts" not in args.kwargs


@pytest.mark.asyncio
async def test_create_volume_routes_to_async_volume_create() -> None:
    sdk_volume = MagicMock()
    sdk_volume.volume_id = "vol-fresh"

    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncVolume.create = AsyncMock(return_value=sdk_volume)
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key-abc")
        result = await client.create_volume(name="mcpolis-acme-ups-vol")

    assert result == "vol-fresh"
    e2b_mod.AsyncVolume.create.assert_awaited_once()
    call_args = e2b_mod.AsyncVolume.create.await_args
    assert call_args is not None
    # First positional arg is the name; api_key threads through opts.
    assert call_args.args == ("mcpolis-acme-ups-vol",)
    assert call_args.kwargs["api_key"] == "key-abc"


@pytest.mark.asyncio
async def test_destroy_volume_routes_to_async_volume_destroy() -> None:
    with patch(
        "mcpolis.adapters.sandbox_e2b.real_client._import_sdk",
    ) as mock_import:
        e2b_mod = MagicMock()
        e2b_mod.AsyncVolume.destroy = AsyncMock(return_value=True)
        import e2b as real_e2b
        e2b_mod.AuthenticationException = real_e2b.AuthenticationException
        e2b_mod.RateLimitException = real_e2b.RateLimitException
        e2b_mod.NotFoundException = real_e2b.NotFoundException
        mock_import.return_value = e2b_mod

        client = RealE2BClient(api_key="key-abc")
        await client.destroy_volume("vol-bye")

    e2b_mod.AsyncVolume.destroy.assert_awaited_once()
    call_args = e2b_mod.AsyncVolume.destroy.await_args
    assert call_args is not None
    assert call_args.args == ("vol-bye",)
    assert call_args.kwargs["api_key"] == "key-abc"


# ---------- import-error path ----------


def test_missing_e2b_dep_surfaces_as_typed_error() -> None:
    """If the e2b package isn't installed, the lazy import raises
    a typed ``E2BSDKError`` rather than a bare ImportError that
    would crash the caller's exception-handling path."""
    from mcpolis.adapters.sandbox_e2b.real_client import _import_sdk

    with patch(
        "builtins.__import__",
        side_effect=ImportError("No module named 'e2b'"),
    ):
        with pytest.raises(E2BSDKError) as exc:
            _import_sdk()
    assert "e2b SDK is not installed" in exc.value.detail
