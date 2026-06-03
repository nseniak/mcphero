"""Diagnostic for the ``double_reattach`` failure.

Hypothesis: after the first auto-pause/resume cycle, E2B does NOT
schedule another auto-pause within the configured idle window — so
the sandbox stays running indefinitely after the first resume, and
the second 35s sleep in ``double_reattach`` produces no second
pause to react to.

Test: open a session with ``on_timeout=30s``, force the first auto-
pause, observe it via ``client.list_sandboxes`` (state should flip
``running`` → ``paused``). After reattach, sleep again and POLL the
sandbox state — if it stays ``running`` indefinitely, hypothesis
confirmed; if it eventually flips to ``paused``, our reattach code
must be missing the second event.

Run:
    export MCPOLIS_E2B_API_KEY=...
    python backend/tests/integration/diagnose_double_reattach.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from io import StringIO
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mcp.client.session import ClientSession  # noqa: E402

from mcpolis.adapters.sandbox_e2b import (  # noqa: E402
    E2BSandboxService,
    RealE2BClient,
)
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer  # noqa: E402
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig  # noqa: E402
from mcpolis.domain.model.upstream import (  # noqa: E402
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.services.sandbox_service import SandboxResources  # noqa: E402

API_KEY = os.environ.get("MCPOLIS_E2B_API_KEY") or os.environ.get("E2B_API_KEY")
RUN_ID = uuid.uuid4().hex[:8]
IDLE = 30


async def main() -> int:
    if not API_KEY:
        print("MCPOLIS_E2B_API_KEY required", file=sys.stderr)
        return 2

    client = RealE2BClient(api_key=API_KEY)
    service = E2BSandboxService(
        client,
        mcpolis_instance=f"diag-{RUN_ID}",
        on_timeout_seconds=IDLE,
    )
    upstream = UpstreamDefinition(
        id=f"diag-{RUN_ID}",
        display_name=f"diag-{RUN_ID}",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-everything"],
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    resources = SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)
    log_buffer = LogBuffer()

    async def poll_state(label: str) -> str:
        # Reach past the handle abstraction onto the SDK's raw
        # ``ListedSandbox`` so we can read ``end_at`` — the moment
        # E2B has scheduled this sandbox to expire/pause. The
        # difference between ``end_at`` and *now* is the live
        # remaining timeout, regardless of what we thought we set.
        infos = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": f"diag-{RUN_ID}"},
        )
        if not infos:
            return f"{label}: no sandbox found"
        from datetime import datetime, timezone  # noqa: PLC0415
        now = datetime.now(tz=timezone.utc)
        parts: list[str] = []
        for i in infos:
            raw = getattr(i, "_raw", None)
            end_at = getattr(raw, "end_at", None) if raw is not None else None
            if end_at is not None:
                try:
                    remaining = int((end_at - now).total_seconds())
                except Exception:
                    remaining = None
            else:
                remaining = None
            tag = f"timeout_remaining~{remaining}s" if remaining is not None else "no end_at"
            parts.append(f"{i.sandbox_id}={i.state} {tag}")
        return f"{label}: {', '.join(parts)}"

    print(f"# diag run {RUN_ID}, idle={IDLE}s")
    session_id = f"diag-{RUN_ID}-double"
    async with service.session(
        session_id=session_id,
        org_id=f"acme-{RUN_ID}",
        upstream=upstream,
        resources=resources,
        denylist=(),
        errlog=cast(StringIO, log_buffer),
    ) as session:
        read_stream, write_stream = session.read_stream, session.write_stream
        session = ClientSession(read_stream, write_stream)
        async with session:
            t0 = time.monotonic()
            await asyncio.wait_for(session.initialize(), timeout=120.0)
            print(f"  init: {time.monotonic()-t0:.1f}s")
            print(f"  {await poll_state('post-init')}")

            # Cycle 1: provoke first auto-pause.
            print(f"\n# Cycle 1: sleeping {IDLE+5}s to provoke pause...")
            await asyncio.sleep(IDLE + 5)
            print(f"  {await poll_state('post-sleep-1')}")
            t1 = time.monotonic()
            await asyncio.wait_for(session.list_tools(), timeout=60.0)
            print(f"  list_tools after pause-1: {time.monotonic()-t1:.2f}s")
            print(f"  {await poll_state('post-list-1')}")

            # Cycle 2: poll state during the sleep at 5s intervals so
            # we catch when (or whether) it transitions to paused.
            print(f"\n# Cycle 2: sleeping with state polling every 5s...")
            cycle_2_start = time.monotonic()
            for _tick in range(0, IDLE + 30, 5):
                await asyncio.sleep(5)
                elapsed = time.monotonic() - cycle_2_start
                print(f"  t+{elapsed:.0f}s  {await poll_state('poll')}")
            t2 = time.monotonic()
            await asyncio.wait_for(session.list_tools(), timeout=60.0)
            print(f"\n  list_tools after sleep-2: {time.monotonic()-t2:.2f}s")
            print(f"  {await poll_state('post-list-2')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
