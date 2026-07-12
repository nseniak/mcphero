"""Diagnostic: reproduce a false ``SubprocessExitedDuringInit`` on the
plain per-tool-call lazy-connect path.

Background: three production incidents (unleash, sentry, influxdb —
see MCPOLIS-BACKEND-11/-X/-V) all failed the same way. The upstream
had no live in-process session (``deferred_attach``). A real tool call
triggered ``ensure_shared_connected`` -> ``connect_shared``, which
reused the existing (paused) E2B sandbox via the "reuse-on-restart"
path. The E2B-side reconnect itself succeeded in under 2s (``envd_ready``
/ ``reconnect.ok``), but ~69-71s later the connect attempt failed with
``upstream.client.lazy_connect.failed`` / ``lazy_attach_failed``,
consistent with ``SubprocessExitedDuringInit`` firing from
``init_with_exit_race`` (see ``stdio_adapter.py``).

Hypothesis: this is the SAME class of E2B post-reattach flakiness
already root-caused for the mid-session stdin-pump reattach stall
(``test_e2b_reattach_stall_recovery_e2e.py``, citing e2b-dev/E2B
#1128 / #1031 / #875) — a stream-death signal firing even though the
process is still alive — but hitting a DIFFERENT code path that never
got the same fix. ``acquire_and_refresh_with_recovery`` (dashboard
refresh) reconnects on a fresh session and retries; the plain
``ensure_shared_connected`` path used by every ordinary tool call does
not.

This script drives that plain lazy-connect path directly, cycle by
cycle:
  1. Connect the upstream fresh (real create) with a SHORT idle-pause
     timeout, then immediately force-pause the live sandbox (no
     waiting for the idle timer — the E2B SDK pause call is instant).
  2. Simulate a cold ``deferred_attach`` reconnect the way a real
     mcpolis process would see it after a restart, or after this
     upstream simply hadn't been touched since boot: build a BRAND
     NEW ``UpstreamClientManager`` / ``E2BSandboxService`` pointed at
     the SAME persistence store (so it resolves the existing, paused
     sandbox via "reuse-on-restart"), and call
     ``ensure_shared_connected`` — the exact method a real tool call
     goes through (``tool_router`` -> ``upstream_connection_service``
     -> ``client_manager.ensure_shared_connected``).
  3. Time it and record whether it raised (and what).
  4. On success, pause the new live sandbox again for the next cycle.

Per the existing stress test's own notes, the underlying E2B
post-pause stream flakiness reproduces roughly 50-75% of the time when
forced this way, so a handful of cycles should be enough to catch it.

Run:
    export MCPOLIS_E2B_API_KEY=... (or E2B_API_KEY)
    python backend/tests/integration/diagnose_lazy_connect_false_exit.py [cycles] [settle_seconds]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.normpath(os.path.join(_HERE, ".."))
_SRC = os.path.normpath(os.path.join(_TESTS, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (  # noqa: E402
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService, RealE2BClient  # noqa: E402
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError  # noqa: E402
from mcpolis.adapters.upstream_clients.client_manager import (  # noqa: E402
    UpstreamClientManager,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver  # noqa: E402
from unit.factories import make_upstream_definition  # noqa: E402

API_KEY = os.environ.get("MCPOLIS_E2B_API_KEY") or os.environ.get("E2B_API_KEY")
RUN_ID = uuid.uuid4().hex[:8]
INSTANCE = f"diag-lazy-{RUN_ID}"
ORG_ID = f"acme-{RUN_ID}"
IDLE_SECONDS = 120  # not actually waited out — we pause explicitly


def make_manager(
    client: RealE2BClient,
    persistence: InMemorySandboxPersistenceRepository,
    upstream: Any,
) -> tuple[UpstreamClientManager, E2BSandboxService]:
    """Build a FRESH manager+service pair, as a real mcpolis process
    boot would, sharing the persistence store so it reattaches to
    whatever sandbox is already on record for this org/upstream."""
    service = E2BSandboxService(
        client,
        mcpolis_instance=INSTANCE,
        on_timeout_seconds=IDLE_SECONDS,
        persistence=persistence,
        reuse_sandboxes_on_restart=True,
    )
    manager = UpstreamClientManager(
        upstreams=[upstream],
        org_id=ORG_ID,
        sandbox_services={"e2b": service},
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
    )
    return manager, service


async def pause_live_sandbox(
    client: RealE2BClient,
    service: E2BSandboxService,
    manager: UpstreamClientManager,
    upstream_id: str,
    *,
    settle_seconds: float,
) -> bool:
    """Force-pause the currently-live sandbox so the NEXT connect on a
    fresh manager must reattach, without waiting for the idle timer.

    A first attempt at this diagnostic paused and immediately
    reconnected, and every cycle came back fast with
    ``was_paused=False`` on the envd-ready probe — i.e. we never
    actually observed a genuinely-settled pause; ``handle.pause()``
    returning just means E2B accepted the request, not that the
    sandbox has durably frozen. Production incidents only ever hit
    upstreams that had been sitting paused for HOURS. So: poll
    ``list_sandboxes`` until E2B itself reports ``paused``, then sleep
    an extra ``settle_seconds`` buffer before letting the caller
    reconnect, to actually approximate a "been paused a while" wake.
    """
    state = manager.get_state(upstream_id)
    task = state.shared_task if state is not None else None
    session_id = getattr(task, "_session_id", None)
    if session_id is None:
        return False
    handle = service._live_sandboxes.get(session_id)  # type: ignore[reportPrivateUsage]
    if handle is None:
        return False
    sandbox_id = await handle.pause()

    deadline = time.monotonic() + 30.0
    settled = False
    while time.monotonic() < deadline:
        infos = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": INSTANCE},
        )
        matched = [i for i in infos if i.sandbox_id == sandbox_id]
        if matched and matched[0].state == "paused":
            settled = True
            break
        await asyncio.sleep(1.0)
    if not settled:
        print(f"  WARNING: sandbox {sandbox_id} never reported paused within 30s")
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    return True


def is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


async def main() -> int:
    if not API_KEY:
        print("MCPOLIS_E2B_API_KEY (or E2B_API_KEY) required", file=sys.stderr)
        return 2
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    settle_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    client = RealE2BClient(api_key=API_KEY)
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(
        id=f"diag-lazy-{RUN_ID}", command="npx",
    )
    upstream.stdio.args = ["-y", "@modelcontextprotocol/server-everything"]  # type: ignore[union-attr]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    print(f"# diag run {RUN_ID}, cycles={cycles}")
    failures = 0
    exit_related_failures = 0
    manager: UpstreamClientManager | None = None

    try:
        # Cycle 0: genuine fresh create — establishes the sandbox that
        # every later cycle will pause-and-reattach against.
        manager, service = make_manager(client, persistence, upstream)
        t0 = time.monotonic()
        await manager.ensure_shared_connected(upstream)
        print(f"cycle 0 (create): OK in {time.monotonic() - t0:.1f}s")
        paused = await pause_live_sandbox(
            client, service, manager, upstream.id, settle_seconds=settle_seconds,
        )
        print(f"  paused={paused}")

        for cycle in range(1, cycles + 1):
            manager, service = make_manager(client, persistence, upstream)
            t0 = time.monotonic()
            try:
                await manager.ensure_shared_connected(upstream)
                elapsed = time.monotonic() - t0
                print(f"cycle {cycle}: reconnect OK in {elapsed:.1f}s")
                ok = True
            except Exception as exc:  # noqa: BLE001 - diagnostic, want every shape
                elapsed = time.monotonic() - t0
                failures += 1
                name = type(exc).__name__
                if name in {"SubprocessExitedDuringInit", "StdioInitTimeout"}:
                    exit_related_failures += 1
                print(
                    f"cycle {cycle}: reconnect FAILED after {elapsed:.1f}s "
                    f"-> {name}: {exc}",
                )
                ok = False

            if ok:
                paused = await pause_live_sandbox(
                    client, service, manager, upstream.id,
                    settle_seconds=settle_seconds,
                )
                print(f"  paused={paused}")
            else:
                # Leave it — the persisted ref may still resolve to a
                # live-but-paused sandbox next cycle; if not, the next
                # ``ensure_shared_connected`` will fall back to a fresh
                # create and the cycle count under-reports attempts.
                pass

        print(
            f"\nDONE: {failures}/{cycles} reconnect cycles failed "
            f"({exit_related_failures} were SubprocessExitedDuringInit/"
            "StdioInitTimeout)",
        )
        return 1 if exit_related_failures else 0
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            print(
                "mcpolis E2B templates not published on the active account — "
                "run `cd runner/e2b-templates && make build`.",
                file=sys.stderr,
            )
            return 2
        raise
    finally:
        if manager is not None:
            try:
                await manager.disconnect_upstream(upstream.id)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
