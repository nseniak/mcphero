"""Targeted real-SDK recovery guardrails for the E2B backend (E2B-T).

These are paid, on-demand integration tests. Each talks to the live
E2B API (``Sandbox.create`` / ``commands.connect`` / ``pause`` /
``kill`` / ``list``) and is gated off the default suite by the
standard ``E2B_API_KEY`` skip marker — they run under
``backend/run-integration-tests.sh`` only when a key is present.

Scope (one targeted scenario per failure class the mock suite
can't pin against real SDK shapes):

* **E2B-T1** — ``set_timeout`` value survives TWO reattach cycles
  (E2B's auto-resume otherwise resets the idle window to the SDK's
  300s default, defeating the ``MCPOLIS_E2B_IDLE_PAUSE_SECONDS``
  cost knob).
* **E2B-T2** — a sandbox killed mid-tool-call sets ``transport_failed``
  and the next acquire heals onto a fresh sandbox.
* **E2B-T3** — a materialize-file write to a read-only path surfaces
  as a clean connect failure, not a hang.
* **E2B-T4** — the startup reconciler kills a metadata-tagged orphan
  while keeping the persisted (recognized) sandbox, against the real
  ``list_sandboxes`` paginator + metadata filter.

Cost: ~$0.005-0.02 of E2B compute per test; ~$0.05 for the file.
Every sandbox is tagged with a per-run UUID and killed in a
``finally`` block so a parallel CI job never sees another job's
sandboxes and a flaky test can't leak compute.

To run::

    cd runner/e2b-templates && make build      # one-time, ~15 min
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh \
        tests/integration/test_e2b_targeted_recovery_e2e.py -v -s
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
import os
import time
import uuid
from datetime import UTC, datetime
from io import StringIO
from typing import cast

import pytest
from mcp import types as mcp_types
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService, RealE2BClient
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.reconciler import E2BSandboxReconciler
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.tool_registry import SEPARATOR
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    SandboxResources,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    acquire_upstream_session,
)
from tests.integration._e2b_log_capture import (
    events_since,
    stream_death_events_since,
)
from tests.unit.factories import make_upstream_definition


# Reattach-event capture lives in the shared ``_e2b_log_capture`` module
# (imported above): one process-global ``structlog.configure`` for every
# integration file, so a per-file configure can't clobber a sibling's
# capture list (the bug that left M4 empty under ``--dist loadfile``).
E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK targeted recovery tests skipped",
)

_SERVER_URL = "http://localhost:8000"
# Short idle window so the reattach scenarios provoke an E2B
# auto-pause in seconds, not the production-default 5 min. +5s past
# the deadline gives a consistent reproduction (auto-pause fires
# within a few seconds of the configured timeout).
IDLE_PAUSE_SECONDS = 30
REATTACH_WAIT_SECONDS = IDLE_PAUSE_SECONDS + 5
INITIALIZE_TIMEOUT = 120.0
TOOL_CALL_TIMEOUT = 30.0


def make_test_metadata(scenario: str) -> dict[str, str]:
    """Tag every sandbox so we can find + clean up our own work even
    when a test crashes mid-flight. ``scenario`` distinguishes
    sandboxes from different test functions in the same run."""
    return {
        "mcpolis_test": "1",
        "test_run_id": TEST_RUN_ID,
        "scenario": scenario,
    }


def make_default_resources() -> SandboxResources:
    """Smallest published combo to keep per-test cost minimal.
    Matches mcpolis-node-cpu1-ram1024 / mcpolis-python-cpu1-ram1024."""
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


def make_everything_upstream(suffix: str) -> UpstreamDefinition:
    """server-everything over npx — the node template's pre-warmed
    'kitchen sink' MCP."""
    upstream = make_upstream_definition(
        id=f"e2e-{suffix}-{TEST_RUN_ID}", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]
    return upstream


def make_e2b_service(
    *,
    instance: str,
    persistence: InMemorySandboxPersistenceRepository | None = None,
    reuse_on_restart: bool = False,
    on_timeout_seconds: int = IDLE_PAUSE_SECONDS,
) -> E2BSandboxService:
    assert E2B_API_KEY is not None  # guarded by pytestmark
    return E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=instance,
        on_timeout_seconds=on_timeout_seconds,
        persistence=persistence,
        reuse_sandboxes_on_restart=reuse_on_restart,
    )


def make_e2b_manager(
    upstream: UpstreamDefinition, org_id: str, service: E2BSandboxService,
) -> UpstreamClientManager:
    return UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_services={"e2b": service},
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
    )


def make_test_client() -> RealE2BClient:
    assert E2B_API_KEY is not None  # guarded by pytestmark
    return RealE2BClient(api_key=E2B_API_KEY)


def is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


# ---------------------------------------------------------------------------
# E2B-T1 — set_timeout survives two reattach cycles
# ---------------------------------------------------------------------------


# Two 35s idle windows plus two sandbox opens: the split-per-cycle
# shape the wake fix forced on this test costs roughly 90-110s, which
# sits under the global 120s ceiling on a quiet box and over it when
# the broad matrix is competing for E2B. Raise the ceiling for these
# two rather than shortening the sleeps, which would stop E2B pausing
# at all and make the test prove nothing.
@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_t1_set_timeout_holds_across_two_wake_cycles() -> None:
    """E2B-T1: the configured idle window must survive TWO consecutive
    auto-pause/wake cycles.

    E2B's ``auto_resume`` resets the sandbox timeout to the SDK's 300s
    default, NOT the value passed to ``Sandbox.create``. The service
    re-applies ``set_timeout(on_timeout_seconds)`` whenever it
    reconnects to a persisted sandbox; without that, cycle 2's sleep
    never reaches the (reset) 300s deadline and no second pause fires.

    The drift-proof observable is a SECOND wake after a second sleep:
    only possible if the idle window stayed at ``IDLE_PAUSE_SECONDS``.

    One session per cycle, because a wake now ENDS the session rather
    than being papered over inside it. The service used to reattach to
    the frozen MCP process here; it now retires that process, since a
    process resumed from a snapshot writes into TCP connections that
    were severed while it slept. Opening a session per cycle is what
    the client manager does in production when the transport fails.
    Persistence plus reuse is wired on purpose: cycle 2 must land on
    the SAME paused sandbox, or ``set_timeout`` would be trivially
    correct on a freshly created one and the test would prove nothing.
    ~$0.01 of compute (two pause windows).
    """
    persistence = InMemorySandboxPersistenceRepository()
    service = make_e2b_service(
        instance=f"e2e-t1-{TEST_RUN_ID}",
        persistence=persistence,
        reuse_on_restart=True,
    )
    upstream = make_everything_upstream("t1")
    errlog = LogBuffer()
    org_id = f"acme-t1-{TEST_RUN_ID}"

    async def one_cycle(cycle: int) -> None:
        """Open a session, idle past the window, prove it paused."""
        session_id = f"e2e-t1-{TEST_RUN_ID}-c{cycle}"
        async with service.session(
            session_id=session_id,
            org_id=org_id,
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as sandbox_session:
            client_session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with client_session:
                await asyncio.wait_for(
                    client_session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                # Nothing touches the session across this sleep, so a
                # stream death inside the window can only be the pause.
                idle_cursor = time.monotonic_ns()
                await asyncio.sleep(REATTACH_WAIT_SECONDS)
                assert stream_death_events_since(idle_cursor), (
                    f"cycle {cycle}: the sandbox did not pause within "
                    "the idle window"
                )
                assert sandbox_session.transport_failed is not None
                assert sandbox_session.transport_failed.is_set(), (
                    f"cycle {cycle}: the pause must mark the transport "
                    "dead on its own, so the next request is rebuilt "
                    "onto a fresh process instead of being lost"
                )
            service.mark_session_preserve_on_close(session_id)
    try:
        # Cycle 1 proves the sandbox pauses at all.
        await one_cycle(1)
        # Cycle 2 only re-pauses if the reconnect's set_timeout put the
        # idle window back at IDLE_PAUSE_SECONDS. A 300s reset would
        # mean the sleep never trips the deadline, and cycle 2's own
        # assertion inside ``one_cycle`` would fail.
        await one_cycle(2)
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active account — "
                "run `cd runner/e2b-templates && make build`.",
            )
        tail = errlog.get_output()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise
    finally:
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        if ref is not None and ref.sandbox_id is not None:
            with contextlib.suppress(Exception):
                await RealE2BClient(
                    api_key=cast(str, E2B_API_KEY),
                ).kill_sandbox(ref.sandbox_id)


# ---------------------------------------------------------------------------
# E2B-T5 — the ORIGINAL production bug: an MCP that holds an outbound
#          keep-alive connection must work on the FIRST call after a wake
# ---------------------------------------------------------------------------


# A minimal stdio MCP server that does the one thing every server in our
# fixtures avoids: it talks to the internet, over a client that POOLS the
# connection. That pooling is the entire bug. A sandbox snapshot severs
# the socket while the process is frozen; the process cannot tell, writes
# into it on the next request, and gets ECONNRESET. Production saw that
# as one opaque tool failure per pooled socket on the first calls after a
# wake (Sentry MCPOLIS-BACKEND-16), and no test caught it because
# server-everything / filesystem / memory answer entirely from memory.
_POOLING_MCP_JS = r"""
const https = require('node:https');
const TARGET = process.env.PROBE_TARGET_URL;
const agent = new https.Agent({
  keepAlive: true, keepAliveMsecs: 600000, maxSockets: 1,
});

function fetchOnce() {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const req = https.request(TARGET, { agent, method: 'GET' }, (res) => {
      res.resume();
      res.on('end', () => resolve({
        ok: true, status: res.statusCode,
        reused: req.reusedSocket === true, ms: Date.now() - t0,
      }));
    });
    req.on('error', (e) => resolve({
      ok: false, code: e.code || null, reused: req.reusedSocket === true,
      ms: Date.now() - t0, msg: String(e.message || e),
    }));
    req.end();
  });
}

function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

async function handle(msg) {
  if (msg.method === 'initialize') {
    send({ jsonrpc: '2.0', id: msg.id, result: {
      protocolVersion: msg.params.protocolVersion,
      capabilities: { tools: {} },
      serverInfo: { name: 'pooling-probe', version: '1.0.0' },
    }});
  } else if (msg.method === 'tools/list') {
    send({ jsonrpc: '2.0', id: msg.id, result: { tools: [{
      name: 'poolfetch',
      description: 'GET a URL over a pooled keep-alive connection',
      inputSchema: { type: 'object', properties: {} },
    }]}});
  } else if (msg.method === 'tools/call') {
    const r = await fetchOnce();
    send({ jsonrpc: '2.0', id: msg.id, result: {
      content: [{ type: 'text', text: JSON.stringify(r) }],
      isError: !r.ok,
    }});
  } else if (msg.id !== undefined && msg.id !== null) {
    send({ jsonrpc: '2.0', id: msg.id, error:
      { code: -32601, message: 'method not found' } });
  }
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { continue; }
    handle(msg);
  }
});
"""

_PROBE_TARGET = "https://www.google.com/generate_204"
PROBE_USER = "probe@example.com"


def make_pooling_upstream(suffix: str) -> UpstreamDefinition:
    upstream = make_upstream_definition(
        id=f"e2e-{suffix}-{TEST_RUN_ID}", command="node",
    )
    # Passed inline via ``node -e`` rather than materialized as a
    # Sandbox file: the file route goes through SandboxFileRepository,
    # which the manager owns, and wiring a repo here would test the
    # plumbing instead of the wake. ~2KB of argv is well inside limits.
    upstream.stdio.args = ["-e", _POOLING_MCP_JS]  # type: ignore[union-attr]
    upstream.stdio.env = {  # type: ignore[union-attr]
        "PROBE_TARGET_URL": _PROBE_TARGET,
    }
    return upstream


# Two idle windows plus two session opens; see the note on E2B-T1.
@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_t5_pooled_connection_survives_a_wake() -> None:
    """The regression gate for the bug that started all of this.

    Call the tool once so the MCP server has a live pooled connection.
    Let the sandbox pause. Then call again and require the FIRST call
    after the wake to SUCCEED.

    Before the fix this failed with ``ECONNRESET`` on a reused socket,
    reproducibly, 20 wakes out of 20 (see
    ``diagnose_wake_network.py``). It passes now because the wake
    retires the frozen process instead of handing it back, so the
    replacement opens a fresh connection.

    This is the only test in the suite whose MCP server talks to the
    internet. That property is exactly what every other fixture lacks,
    and exactly why the production bug reached a customer.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service = make_e2b_service(
        instance=f"e2e-t5-{TEST_RUN_ID}",
        persistence=persistence,
        reuse_on_restart=True,
    )
    upstream = make_pooling_upstream("t5")
    org_id = f"acme-t5-{TEST_RUN_ID}"
    errlog = LogBuffer()

    manager = make_e2b_manager(upstream, org_id, service)
    registry = ToolRegistry([upstream], manager)
    audit_dir = Path(tempfile.mkdtemp())
    audit = FileAuditRepository(log_path=audit_dir / "audit.jsonl")
    router = ToolRouter(
        registry, manager, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    def payload_of(result: mcp_types.CallToolResult) -> dict[str, object]:
        block = result.content[0]
        assert isinstance(block, mcp_types.TextContent)
        parsed: object = json.loads(block.text)
        assert isinstance(parsed, dict)
        return cast("dict[str, object]", parsed)

    try:
        # Drive the real gateway path, not a raw session. That matters:
        # a wake ENDS the session by design, and the thing that rebuilds
        # it and re-sends the request is the router's stall recovery. A
        # bare ClientSession would just see "Connection closed", which
        # proves nothing about what a user experiences.
        await manager.connect_shared(upstream)
        await registry.refresh_upstream(upstream.id)

        # Cycle 1 warms the pool: after this the MCP server holds an
        # open socket, which is the state the snapshot then freezes.
        warm = await asyncio.wait_for(
            router.route_call(
                org_id=org_id,
                prefixed_name=f"{upstream.id}{SEPARATOR}poolfetch",
                arguments={}, user_id=PROBE_USER, session_id=None,
            ),
            timeout=TOOL_CALL_TIMEOUT * 2,
        )
        warm_payload = payload_of(warm)
        assert warm_payload.get("ok") is True, (
            f"warm-up call must succeed: {warm_payload}"
        )

        # Idle past the pause window, then call ONCE.
        await asyncio.sleep(REATTACH_WAIT_SECONDS)
        after_wake = await asyncio.wait_for(
            router.route_call(
                org_id=org_id,
                prefixed_name=f"{upstream.id}{SEPARATOR}poolfetch",
                arguments={}, user_id=PROBE_USER, session_id=None,
            ),
            timeout=TOOL_CALL_TIMEOUT * 4,
        )
        assert not after_wake.isError, (
            "the first call after a wake must reach the user as a "
            f"success; got {after_wake.content}. An opaque 'Upstream "
            "tool call failed' here is the production symptom back"
        )
        wake_payload = payload_of(after_wake)
        assert wake_payload.get("ok") is True, (
            "the first call after a wake must succeed. "
            f"got {wake_payload} — a 'code': 'ECONNRESET' with "
            "'reused': true is the original production bug back: the "
            "frozen process was handed back holding a dead socket"
        )
        assert wake_payload.get("reused") is not True, (
            "the replacement process must open its own connection; a "
            "reused socket here means a frozen process survived"
        )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active "
                "account — run `cd runner/e2b-templates && make build`.",
            )
        tail = errlog.get_output()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise
    finally:
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        if ref is not None and ref.sandbox_id is not None:
            with contextlib.suppress(Exception):
                await RealE2BClient(
                    api_key=cast(str, E2B_API_KEY),
                ).kill_sandbox(ref.sandbox_id)


# ---------------------------------------------------------------------------
# E2B-T6 — a heal keeps the warm sandbox instead of rebuilding it
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_t6_heal_keeps_the_same_sandbox() -> None:
    """A heal must replace the MCP process, not the sandbox.

    Reusing the sandbox is where the saving lives: the expensive part
    of a cold start is downloading the MCP's package (7-22s per server
    in production, against ~3s when it is already on disk), and that
    cache lives on the sandbox filesystem.

    This asserts the OUTCOME, not the intention. An independent review
    found the first version of the fix asked for the sandbox to be
    kept, logged that it had been kept, and destroyed it anyway: the
    heal's close-then-open tore the session down with
    ``preserve=False``, which deleted the ref and killed the sandbox
    before the reopen could read it. Every unit test passed, because
    the fake sandbox service has no sandbox to kill. Only a real
    sandbox id, compared before and after, catches that.
    """
    org_id = f"acme-t6-{TEST_RUN_ID}"
    instance = f"e2e-t6-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    service = make_e2b_service(
        instance=instance, persistence=persistence, reuse_on_restart=True,
    )
    upstream = make_everything_upstream("t6")
    manager = make_e2b_manager(upstream, org_id, service)

    try:
        await manager.connect_shared(upstream)
        before = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert before is not None and before.sandbox_id is not None
        sandbox_before = before.sandbox_id
        pid_before = before.pid

        creates_cursor = time.monotonic_ns()
        await manager.reconnect_shared_fresh(upstream)

        after = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert after is not None and after.sandbox_id is not None
        assert after.sandbox_id == sandbox_before, (
            "the heal must keep the warm sandbox; a different id means "
            "it was destroyed and rebuilt, so every wake pays a full "
            "package download"
        )
        assert not events_since("sandbox.e2b.create", creates_cursor), (
            "no fresh sandbox may be created by a heal that had a "
            "healthy one to reuse"
        )
        assert after.pid != pid_before, (
            "the MCP process MUST be replaced even though the sandbox "
            "is not — reusing a process across a pause is the bug"
        )

        # And the healed session actually works.
        session = manager.get_session(upstream.id)
        result = await asyncio.wait_for(
            session.list_tools(), timeout=TOOL_CALL_TIMEOUT,
        )
        assert result.tools
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active "
                "account — run `cd runner/e2b-templates && make build`.",
            )
        raise
    finally:
        with contextlib.suppress(Exception):
            await manager.stop_all()
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        if ref is not None and ref.sandbox_id is not None:
            with contextlib.suppress(Exception):
                await RealE2BClient(
                    api_key=cast(str, E2B_API_KEY),
                ).kill_sandbox(ref.sandbox_id)


# ---------------------------------------------------------------------------
# E2B-T7 — a cpu/ram edit forces a new sandbox instead of reusing the old size
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_t7_resize_forces_a_fresh_sandbox() -> None:
    """Sandbox size is fixed at create time, so reuse must refuse it.

    A reconnect attaches by ``sandbox_id`` and cannot re-size. Reusing
    after the operator raised memory would keep running at the OLD
    size while `connect_shared` re-persists the config hash and clears
    the dirty-config banner — a dashboard asserting something false.

    The unit twin (`test_reuse_is_refused_when_the_size_changed`)
    proves the branch is taken against a mock. This proves E2B
    actually hands back a bigger machine, which is the part a mock
    cannot tell you.
    """
    org_id = f"acme-t7-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    service = make_e2b_service(
        instance=f"e2e-t7-{TEST_RUN_ID}",
        persistence=persistence,
        reuse_on_restart=True,
    )
    upstream = make_everything_upstream("t7")
    small = SandboxResources(cpu_vcpus=1, memory_mb=1024, disk_gb=0)
    large = SandboxResources(cpu_vcpus=2, memory_mb=2048, disk_gb=0)
    first_sandbox_id: str | None = None

    async def open_at(resources: SandboxResources, tag: str) -> None:
        session_id = f"e2e-t7-{TEST_RUN_ID}-{tag}"
        async with service.session(
            session_id=session_id, org_id=org_id, upstream=upstream,
            resources=resources, denylist=(),
        ) as sandbox_session:
            client = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with client:
                await asyncio.wait_for(
                    client.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
            service.mark_session_preserve_on_close(session_id)

    try:
        await open_at(small, "small")
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert ref is not None and ref.sandbox_id is not None
        first_sandbox_id = ref.sandbox_id
        assert ref.metadata.get("e2b_template") == (
            "mcpolis-node-cpu1-ram1024"
        ), f"the ref must record the size it was built at; got {ref.metadata}"

        # Same size: the warm sandbox is reused, which is the saving
        # this check must not throw away.
        await open_at(small, "same")
        ref_same = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        assert ref_same is not None
        assert ref_same.sandbox_id == first_sandbox_id, (
            "an unchanged size must still reuse the warm sandbox"
        )

        # Bigger: reuse must be refused and a new sandbox created.
        await open_at(large, "large")
        ref_big = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert ref_big is not None and ref_big.sandbox_id is not None
        assert ref_big.sandbox_id != first_sandbox_id, (
            "a size change must fresh-create; reusing the old sandbox "
            "keeps the old size while the dashboard reports the new one"
        )
        assert ref_big.metadata.get("e2b_template") == (
            "mcpolis-node-cpu2-ram2048"
        ), f"the new sandbox must be the requested size; got {ref_big.metadata}"
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active "
                "account — run `cd runner/e2b-templates && make build`.",
            )
        raise
    finally:
        for sid in {first_sandbox_id} | {
            (await persistence.get(
                org_id=org_id, upstream_id=upstream.id,
            ) or SandboxPersistedRef(
                provider="e2b", org_id=org_id, upstream_id=upstream.id,
                mcpolis_instance="x", sandbox_id=None,
                paused_snapshot_id=None, pid=None, metadata={},
                cached_server_info=None, cached_self_description=None,
                last_updated=datetime.now(UTC),
            )).sandbox_id,
        }:
            if sid is None:
                continue
            with contextlib.suppress(Exception):
                await RealE2BClient(
                    api_key=cast(str, E2B_API_KEY),
                ).kill_sandbox(sid)


# ---------------------------------------------------------------------------
# E2B-T2 — sandbox killed mid-tool-call → transport_failed + heal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_kill_mid_call_marks_transport_failed_then_heals() -> None:
    """E2B-T2: an external ``kill_sandbox`` while a ``call_tool`` is in
    flight must (a) fail that call fast and flip ``transport_failed``,
    and (b) leave the next acquire able to heal onto a fresh sandbox.

    Drives the production shared-session path
    (``acquire_upstream_session`` + ``ToolRegistry.refresh_upstream``)
    exactly like ``test_e2b_zombie_session_heal_e2e.py``, but kills the
    sandbox DURING an in-flight tool call rather than between refreshes
    — the harsher race where the streaming RPC dies mid-request.
    ~$0.02 of compute.
    """
    org_id = f"acme-t2-{TEST_RUN_ID}"
    instance = f"e2e-t2-{TEST_RUN_ID}"
    upstream = make_everything_upstream("t2")
    client = make_test_client()
    service = make_e2b_service(instance=instance)
    manager = make_e2b_manager(upstream, org_id, service)
    registry = ToolRegistry([upstream], manager)
    upstream_id = upstream.id

    try:
        # 1) Healthy connect: a live shared session with a usable
        #    sandbox + MCP process.
        await manager.connect_shared(upstream)
        session = manager.get_session(upstream_id)
        list_result = await asyncio.wait_for(
            session.list_tools(), timeout=TOOL_CALL_TIMEOUT,
        )
        assert list_result.tools, "server-everything should expose tools"

        # 2) Fire a deliberately slow tool, then kill the sandbox out
        #    from under it mid-flight. ``server-everything`` exposes a
        #    long-running op (renamed across versions, discover it
        #    defensively); fall back to a known name.
        slow_tool = next(
            (t.name for t in list_result.tools if "long-running" in t.name),
            "longRunningOperation",
        )

        async def _kill_during_call() -> None:
            # Give the call a beat to land on the wire, then kill every
            # sandbox tagged to this instance.
            await asyncio.sleep(2.0)
            infos = await client.list_sandboxes(
                metadata_filter={"mcpolis_instance": instance},
            )
            for info in infos:
                try:
                    await client.kill_sandbox(info.sandbox_id)
                except E2BSDKError:
                    pass

        call_task = asyncio.create_task(
            session.call_tool(slow_tool, {"duration": 15, "steps": 5}),
        )
        kill_task = asyncio.create_task(_kill_during_call())
        # The in-flight call must NOT hang forever — it either raises
        # or returns an error once the killed transport surfaces. Cap
        # it so a regression (silent hang) fails the test rather than
        # wedging the suite.
        with pytest.raises(BaseException):
            await asyncio.wait_for(call_task, timeout=60.0)
        await kill_task

        # 3) Recovery. The product detects a dead sandbox on the NEXT
        #    operation — the failed op surfaces the dead stream and marks
        #    the transport — not via a passive flag (exactly what
        #    test_e2b_zombie_session_heal_e2e proves). So after a mid-call
        #    kill, the first post-kill refresh may fail fast as it hits the
        #    dead transport, and the next refresh reconnects a fresh
        #    sandbox. Tolerate one fast failure, then REQUIRE a healthy
        #    refresh; cap each op so a silent-hang regression fails the
        #    test instead of wedging it.
        healed = False
        for _ in range(2):
            try:
                await asyncio.wait_for(
                    acquire_upstream_session(
                        org_id=org_id, upstream=upstream, effective_user="",
                        connection_store=None, client_manager=manager,
                        server_url=_SERVER_URL,
                    ),
                    timeout=30.0,
                )
                await asyncio.wait_for(
                    registry.refresh_upstream(upstream_id), timeout=30.0,
                )
            except Exception:
                # First post-kill op detecting the dead transport — expected.
                continue
            if registry.get_all_tools():
                healed = True
                break
        assert healed, (
            "after a mid-call kill the shared session must heal onto a fresh "
            "sandbox and return tools, not reuse the dead transport"
        )

        # 4) After healing, the transport-dead predicate the manager uses
        #    to decide reconnect-vs-reuse must report the fresh session as
        #    alive (a positive check on the replaced session — the killed
        #    one was detected and dropped by the heal above).
        state = manager.get_state(upstream_id)
        assert state is not None and state.shared_task is not None
        assert state.shared_task.is_transport_alive(), (
            "after healing, the fresh shared session must report a live "
            "transport"
        )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active account — "
                "run `cd runner/e2b-templates && make build`.",
            )
        raise
    finally:
        try:
            await manager.disconnect_upstream(upstream_id)
        except Exception:
            pass
        # Belt-and-suspenders: kill any sandbox still tagged to this run.
        try:
            infos = await client.list_sandboxes(
                metadata_filter={"mcpolis_instance": instance},
            )
            for info in infos:
                try:
                    await client.kill_sandbox(info.sandbox_id)
                except E2BSDKError:
                    pass
        except E2BSDKError:
            pass


# ---------------------------------------------------------------------------
# E2B-T3 — materialize-file failure on a read-only path → clean connect fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_materialize_file_readonly_path_fails_cleanly() -> None:
    """E2B-T3: a Sandbox-file write targeting a read-only location must
    surface as a clean connect failure (a raised ``Exception`` out of
    ``service.session()``), NOT a silent hang or a half-started MCP.

    ``/proc`` is a read-only pseudo-filesystem in every E2B template;
    ``files.write`` (or the chmod that follows) there fails, and the
    pre-exec materialize hook must propagate that as the session's
    connect error. The sandbox is created+torn-down inside
    ``service.session()`` so there's no handle to leak; the test still
    guards with a per-run instance tag and a best-effort sweep.
    ~$0.005 of compute (boot only; the MCP never starts).
    """
    org_id = f"acme-t3-{TEST_RUN_ID}"
    instance = f"e2e-t3-{TEST_RUN_ID}"
    client = make_test_client()
    upstream = make_everything_upstream("t3")
    service = make_e2b_service(instance=instance)

    # ``/proc`` is read-only inside the sandbox; writing a file there
    # must fail the materialize step before the MCP process starts.
    materialize = [
        MaterializeFile(
            name="READONLY_PROBE",
            target_path="/proc/mcpolis-readonly-probe.txt",
            contents=f"should-never-land-{TEST_RUN_ID}",
        ),
    ]
    errlog = StringIO()
    try:
        with pytest.raises(Exception) as exc_info:
            async with service.session(
                session_id=f"e2e-t3-{TEST_RUN_ID}",
                org_id=org_id,
                upstream=upstream,
                resources=make_default_resources(),
                denylist=(),
                errlog=errlog,
                materialize_files=materialize,
            ):
                pass
        # If the raise itself was a template-missing SDK error, that's
        # an environment gap, not a materialize-failure assertion.
        if is_template_missing_error(exc_info.value):
            pytest.skip(
                "mcpolis E2B templates not published on the active account — "
                "run `cd runner/e2b-templates && make build`.",
            )
        # The failure should reference the write/path, not be a generic
        # timeout — a hang would have blown the test's own time budget.
        message = str(exc_info.value).lower()
        assert any(
            token in message
            for token in (
                "proc", "permission", "read-only", "readonly",
                "write", "denied", "no such", "materiali",
            )
        ), (
            "read-only materialize failure should surface a path/write "
            f"error, got: {exc_info.value!r}"
        )
    finally:
        # No live handle is returned by a failed session(), but sweep
        # for any sandbox that booted before the write blew up.
        try:
            infos = await client.list_sandboxes(
                metadata_filter={"mcpolis_instance": instance},
            )
            for info in infos:
                try:
                    await client.kill_sandbox(info.sandbox_id)
                except E2BSDKError:
                    pass
        except E2BSDKError:
            pass


# ---------------------------------------------------------------------------
# E2B-T4 — reconciler against a real account: orphan killed, recognized kept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t4_reconciler_kills_orphan_keeps_recognized() -> None:
    """E2B-T4: the startup reconciler, run against the live account,
    kills a RUNNING orphan (in my instance, not in persistence) while
    leaving a recognized PAUSED snapshot (in my instance, in persistence)
    untouched. The reconciler's documented contract keeps paused snapshots
    and kills running sandboxes (a running sandbox's owning task is gone
    after a restart — a zombie), so the recognized ref is PAUSED before it
    is persisted.

    Both sandboxes carry the same ``mcpolis_instance`` tag and a
    per-run ``test_run_id`` so the reconciler's own
    ``list_sandboxes(metadata_filter={'mcpolis_instance': ...})``
    paginator + metadata filter are exercised end-to-end. Only the paused
    snapshot gets a persistence ref, so the reconciler must classify the
    running orphan as an orphan and kill it.
    ~$0.01 of compute (two tiny sandboxes, no MCP process).
    """
    client = make_test_client()
    instance = f"e2e-t4-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    org_id = f"acme-t4-{TEST_RUN_ID}"

    recognized_id: str | None = None
    recognized_snapshot_id: str | None = None
    orphan_id: str | None = None
    try:
        # Recognized: created, then PAUSED → a snapshot the reconciler
        # recognizes (via paused_snapshot_id) and KEEPS. A running ref
        # would be killed as a post-restart zombie, so the recognized one
        # must be paused.
        recognized = await client.create_sandbox(
            template="base",
            metadata={
                "mcpolis_instance": instance,
                **make_test_metadata("t4-recognized"),
            },
            timeout_seconds=120,
        )
        recognized_id = recognized.sandbox_id
        recognized_snapshot_id = await recognized.pause()
        assert recognized_snapshot_id, "pause returned an empty snapshot id"

        # Orphan: left RUNNING and unpersisted → the reconciler must kill it.
        orphan = await client.create_sandbox(
            template="base",
            metadata={
                "mcpolis_instance": instance,
                **make_test_metadata("t4-orphan"),
            },
            timeout_seconds=120,
        )
        orphan_id = orphan.sandbox_id

        # Persist ONLY the paused snapshot as recognized.
        await persistence.upsert(SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=f"e2e-t4-{TEST_RUN_ID}",
            mcpolis_instance=instance,
            sandbox_id=None,
            paused_snapshot_id=recognized_snapshot_id,
            pid=None,
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime.now(UTC),
        ))

        reconciler = E2BSandboxReconciler(
            client, persistence, mcpolis_instance=instance,
        )
        report = await reconciler.reconcile()

        # The running orphan was killed; the recognized paused snapshot kept.
        assert report.killed_orphan_sandboxes >= 1, (
            f"reconciler should kill the unpersisted running orphan; "
            f"report={report!r}"
        )
        assert report.kept_paused_snapshots >= 1, (
            f"reconciler should keep the recognized paused snapshot; "
            f"report={report!r}"
        )
        # Provider view: the running orphan is gone.
        remaining = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": instance},
        )
        remaining_ids = {info.sandbox_id for info in remaining}
        assert orphan_id not in remaining_ids, (
            "the unpersisted running orphan must be killed by reconcile"
        )
        # The recognized paused snapshot must SURVIVE — verify with the same
        # connect_sandbox round-trip the real-SDK pause/resume test uses,
        # not by assuming the snapshot id appears in list_sandboxes() (a
        # paused sandbox is not guaranteed to be listed under that id).
        resumed = await client.connect_sandbox(recognized_snapshot_id)
        assert resumed.sandbox_id == recognized_snapshot_id
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "base/templates unavailable on the active account",
            )
        raise
    finally:
        for sandbox_id in (
            recognized_id, recognized_snapshot_id, orphan_id,
        ):
            if sandbox_id is not None:
                try:
                    await client.kill_sandbox(sandbox_id)
                except E2BSDKError:
                    pass


# ---------------------------------------------------------------------------
# E2B-T8 — the dashboard's Start racing a lazy attach opens ONE sandbox
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_t8_start_and_lazy_attach_share_one_sandbox() -> None:
    """The dashboard's Start and a tool call's lazy attach, racing on the
    same upstream, must open ONE sandbox between them.

    They used to take different paths. The lazy attach coalesced through a
    lock held at its own call site; Start's connect never took it. Each
    ran its own ``Sandbox.create``, the loser's session was closed as an
    orphan, and its teardown could delete the persisted record of the
    winner's sandbox. The unit tests count opens on a fake; only a real
    create against E2B shows what the account is billed for.
    """
    org_id = f"acme-t8-{TEST_RUN_ID}"
    instance = f"e2e-t8-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    service = make_e2b_service(
        instance=instance, persistence=persistence, reuse_on_restart=True,
    )
    upstream = make_everything_upstream("t8")
    manager = make_e2b_manager(upstream, org_id, service)
    client = make_test_client()

    try:
        creates_cursor = time.monotonic_ns()
        start = asyncio.create_task(manager.connect_shared(upstream))
        # The tool call arrives while Start's sandbox is still coming up.
        await asyncio.sleep(0.5)
        assert not start.done(), "precondition: Start is still connecting"
        lazy = asyncio.create_task(manager.ensure_shared_connected(upstream))
        started, attached = await asyncio.wait_for(
            asyncio.gather(start, lazy), timeout=INITIALIZE_TIMEOUT,
        )

        creates = events_since("sandbox.e2b.create", creates_cursor)
        assert len(creates) == 1, (
            f"one Start and one tool call created {len(creates)} sandboxes "
            "for one upstream"
        )
        assert started is attached, "both callers must end on one session"
        assert manager.get_session(upstream.id) is attached
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert ref is not None and ref.sandbox_id is not None, (
            "the one sandbox must stay recorded, so the next wake reuses it"
        )
        live = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": instance},
        )
        assert [info.sandbox_id for info in live] == [ref.sandbox_id], (
            "exactly one sandbox may run for the upstream, and it must be "
            f"the recorded one; E2B lists {[i.sandbox_id for i in live]}"
        )
        result = await asyncio.wait_for(
            attached.list_tools(), timeout=TOOL_CALL_TIMEOUT,
        )
        assert result.tools
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active "
                "account — run `cd runner/e2b-templates && make build`.",
            )
        raise
    finally:
        with contextlib.suppress(Exception):
            await manager.stop_all()
        # Kill everything this test's instance created, including a second
        # sandbox if the race regressed: the persisted ref only names one.
        with contextlib.suppress(Exception):
            for info in await client.list_sandboxes(
                metadata_filter={"mcpolis_instance": instance},
            ):
                with contextlib.suppress(Exception):
                    await client.kill_sandbox(info.sandbox_id)
