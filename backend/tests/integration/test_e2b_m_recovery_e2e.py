"""Broad-matrix real-SDK recovery guardrails (E2B-M4, M5).

Part of the split E2B broad-matrix suite. These now run in the
standard paid integration leg (``run-integration-tests.sh``,
``make test-all``) whenever ``E2B_API_KEY`` is set and
``NO_INTEGRATION`` is unset — the suite was split across
``test_e2b_m_*_e2e.py`` siblings so ``--dist loadfile`` spreads the
cost across the xdist workers instead of running the whole sweep
serially on one worker.

This file holds the two recovery-contract sweeps: the 2-cycle
reattach-timeout test (M4) and the volume-persistence test (M5). Both
do real pause/resume cycles, so they are slow but comparable; pairing
them balances against the other split files. Cost: ~$0.05-0.10 across
the file (two pause windows for M4 + two sandboxes and a volume for
M5). Every sandbox is tagged with a per-run UUID and killed in a
``finally`` block (or torn down by ``service.session()``).

The structlog capture (``_CAPTURED_EVENTS`` + ``_capture_processor``)
that M4 greps for ``sandbox.e2b.reattach.ok`` lives HERE, not in the
shared helpers: a module-global shared across split files would be
re-polluted under ``--dist loadfile`` (each worker imports a different
file). Keeping it file-local means M4's capture only sees M4's events.
"""
from __future__ import annotations

import asyncio
import time
from io import StringIO
from typing import cast

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from tests.integration._e2b_log_capture import stream_death_events_since
from tests.integration._e2b_broad_matrix_helpers import (
    E2B_API_KEY,
    INITIALIZE_TIMEOUT,
    REATTACH_WAIT_SECONDS,
    TEST_RUN_ID,
    is_template_missing_error,
    make_node_upstream,
    make_resources,
    make_service,
    make_test_client,
    make_test_metadata,
)

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="needs a live E2B account (E2B_API_KEY unset)",
)


# Pause-evidence capture lives in the shared ``_e2b_log_capture`` module: a
# single process-global ``structlog.configure`` serves every integration file
# (a per-file configure would clobber the others — the bug that left M4's
# capture empty under ``--dist loadfile``). ``stream_death_events_since`` is
# imported above.


# ---------------------------------------------------------------------------
# E2B-M4 — idle-pause→reattach across 2 cycles, timeout holds (2nd tier)
# ---------------------------------------------------------------------------


# Two 35s idle windows plus two sandbox opens: the split-per-cycle
# shape the wake fix forced on this test costs roughly 90-110s, which
# sits under the global 120s ceiling on a quiet box and over it when
# the broad matrix is competing for E2B. Raise the ceiling for these
# two rather than shortening the sleeps, which would stop E2B pausing
# at all and make the test prove nothing.
@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_e2b_m4_timeout_holds_across_two_wake_cycles_broad_matrix() -> None:
    """E2B-M4: the broad-matrix variant of E2B-T1, run on a DIFFERENT
    tier (cpu2-ram2048 node) than the targeted test (cpu1-ram1024).

    Proves the reconnect's ``set_timeout`` re-application holds the
    configured idle window across two auto-pause/wake cycles on a
    larger sandbox too (a 300s reset would leave cycle 2 unable to
    re-pause within the sleep).

    One session per cycle: a wake retires the frozen MCP process and
    ends the session, which is what the client manager rebuilds from
    in production. Persistence plus reuse keeps both cycles on the
    same sandbox, without which cycle 2 would create a fresh one and
    prove nothing about the post-resume re-application.
    """
    instance = f"e2e-m4-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    service = make_service(
        instance=instance, persistence=persistence, reuse_on_restart=True,
    )
    upstream = make_node_upstream("m4")
    errlog = LogBuffer()
    org_id = f"acme-m4-{TEST_RUN_ID}"

    async def one_cycle(cycle: int) -> None:
        """Open a session, idle past the window, prove it paused."""
        session_id = f"{instance}-c{cycle}"
        async with service.session(
            session_id=session_id,
            org_id=org_id,
            upstream=upstream,
            resources=make_resources(2.0, 2048),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                # Nothing touches the session across this sleep, so a
                # stream death inside the window can only be the pause.
                idle_cursor = time.monotonic_ns()
                await asyncio.sleep(REATTACH_WAIT_SECONDS)
                assert stream_death_events_since(idle_cursor), (
                    f"cycle {cycle}: the sandbox did not pause within "
                    "the idle window on the cpu2-ram2048 tier"
                )
                assert sandbox_session.transport_failed is not None
                assert sandbox_session.transport_failed.is_set()
            service.mark_session_preserve_on_close(session_id)

    try:
        await one_cycle(1)
        # Cycle 2 only re-pauses if the reconnect's set_timeout put the
        # idle window back; a 300s reset would fail cycle 2's own
        # in-window assertion.
        await one_cycle(2)
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-node-cpu2-ram2048 not published — run the "
                "template grid build first.",
            )
        tail = errlog.get_output()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise


# ---------------------------------------------------------------------------
# E2B-M5 — volume persistence across pause/resume AND across a fresh sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m5_volume_persists_across_pause_and_fresh_sandbox_broad_matrix() -> None:
    """E2B-M5: a persistent volume survives both a pause/resume AND a
    full sandbox kill + fresh-create with the same volume mounted.

    1. Create a volume, mount it at ``/data`` on sandbox A, write a
       marker, pause, resume (reconnect), read the marker back.
    2. Kill sandbox A. Create a fresh sandbox B with the SAME volume
       mounted at ``/data``; read the marker — it must still be there.

    Skips when the account hasn't enabled the Volumes API (an
    account-level toggle, not a code path mcpolis controls)."""
    client = make_test_client()
    instance = f"e2e-m5-{TEST_RUN_ID}"
    volume_id: str | None = None
    sandbox_a_id: str | None = None
    sandbox_b_id: str | None = None
    marker = f"mcpolis-m5-volume-{TEST_RUN_ID}"
    try:
        try:
            volume_id = await client.create_volume(
                name=f"mcpolis-m5-{TEST_RUN_ID}",
            )
        except E2BSDKError as exc:
            if "use of volumes is not enabled" in str(exc):
                pytest.skip(
                    "E2B Volumes API not enabled on this account "
                    "(account-level toggle in the E2B dashboard).",
                )
            raise
        assert volume_id

        captured: list[bytes] = []

        async def on_stdout(b: bytes) -> None:
            captured.append(b)

        async def on_stderr(_b: bytes) -> None:
            pass

        # --- Sandbox A: write marker, pause, resume, read back. ---
        sandbox_a = await client.create_sandbox(
            template="base",
            metadata={"mcpolis_instance": instance, **make_test_metadata("m5-a")},
            timeout_seconds=120,
            volume_mounts={"/data": volume_id},
        )
        sandbox_a_id = sandbox_a.sandbox_id

        write_proc = await sandbox_a.run_command(
            ["sh", "-c", f"echo {marker} > /data/marker.txt"],
            env={}, on_stdout=on_stdout, on_stderr=on_stderr,
        )
        assert await asyncio.wait_for(write_proc.wait(), timeout=20.0) == 0

        snapshot_id = await sandbox_a.pause()
        resumed = await client.connect_sandbox(snapshot_id)
        # ``connect_sandbox`` returns the same underlying sandbox.
        sandbox_a_id = resumed.sandbox_id

        captured.clear()
        read_proc = await resumed.run_command(
            ["cat", "/data/marker.txt"],
            env={}, on_stdout=on_stdout, on_stderr=on_stderr,
        )
        assert await asyncio.wait_for(read_proc.wait(), timeout=20.0) == 0
        after_resume = b"".join(captured).decode("utf-8", errors="replace")
        assert marker in after_resume, (
            f"marker lost across pause/resume: {after_resume!r}"
        )

        # --- Kill A, fresh-create B with the same volume. ---
        await client.kill_sandbox(sandbox_a_id)
        sandbox_a_id = None

        sandbox_b = await client.create_sandbox(
            template="base",
            metadata={"mcpolis_instance": instance, **make_test_metadata("m5-b")},
            timeout_seconds=120,
            volume_mounts={"/data": volume_id},
        )
        sandbox_b_id = sandbox_b.sandbox_id

        captured.clear()
        read_proc_b = await sandbox_b.run_command(
            ["cat", "/data/marker.txt"],
            env={}, on_stdout=on_stdout, on_stderr=on_stderr,
        )
        assert await asyncio.wait_for(read_proc_b.wait(), timeout=20.0) == 0
        after_fresh = b"".join(captured).decode("utf-8", errors="replace")
        assert marker in after_fresh, (
            "marker did not survive into a fresh sandbox with the same "
            f"volume mounted: {after_fresh!r}"
        )
    finally:
        for sandbox_id in (sandbox_a_id, sandbox_b_id):
            if sandbox_id is not None:
                try:
                    await client.kill_sandbox(sandbox_id)
                except E2BSDKError:
                    pass
        if volume_id is not None:
            try:
                await client.destroy_volume(volume_id)
            except E2BSDKError:
                pass
