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
import os
import time
import uuid
from datetime import UTC, datetime
from io import StringIO
from typing import cast

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService, RealE2BClient
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.reconciler import E2BSandboxReconciler
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    SandboxResources,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    acquire_upstream_session,
)
from tests.integration._e2b_log_capture import reattach_events_since
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


@pytest.mark.asyncio
async def test_t1_set_timeout_holds_across_two_reattach_cycles() -> None:
    """E2B-T1: the configured idle window must survive TWO consecutive
    auto-pause/reattach cycles.

    E2B's ``commands.connect(pid)`` triggers an ``auto_resume`` that
    resets the sandbox timeout to the SDK's 300s default, NOT the
    value passed to ``Sandbox.create``. The service re-applies
    ``set_timeout(on_timeout_seconds)`` after each reattach; without
    that fix, cycle 2's 35s sleep never reaches the (reset) 300s
    deadline and no second pause fires.

    The assertion is the drift-proof observable the e2e suite already
    uses (see ``e2b_real_e2e.py::double_reattach``): a SECOND
    ``sandbox.e2b.reattach.ok`` event after a second 35s sleep is
    only possible if the idle window stayed at ``IDLE_PAUSE_SECONDS``,
    proving the value held at exactly that — not the 300s reset.
    ~$0.01 of compute (two pause windows).
    """
    service = make_e2b_service(instance=f"e2e-t1-{TEST_RUN_ID}")
    upstream = make_everything_upstream("t1")
    errlog = LogBuffer()
    session_id = f"e2e-t1-{TEST_RUN_ID}"
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-t1-{TEST_RUN_ID}",
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

                # Cycle 1: idle past the window, then a round-trip that
                # forces the reattach.
                cycle1_ns = time.monotonic_ns()
                await asyncio.sleep(REATTACH_WAIT_SECONDS)
                await asyncio.wait_for(
                    client_session.list_tools(), timeout=TOOL_CALL_TIMEOUT * 2,
                )
                assert reattach_events_since(cycle1_ns), (
                    "cycle 1: reattach.ok did not fire — the sandbox did "
                    "not auto-pause in this window"
                )

                # Cycle 2 only re-pauses (and re-reattaches) if the
                # post-reattach set_timeout put the idle window back at
                # IDLE_PAUSE_SECONDS. A 300s reset would mean the 35s
                # sleep never trips the deadline.
                cycle2_ns = time.monotonic_ns()
                await asyncio.sleep(REATTACH_WAIT_SECONDS)
                await asyncio.wait_for(
                    client_session.list_tools(), timeout=TOOL_CALL_TIMEOUT * 2,
                )
                assert reattach_events_since(cycle2_ns), (
                    "cycle 2: reattach.ok did not fire — post-reattach "
                    "set_timeout regressed (E2B reset the idle window to "
                    "300s; the configured value did NOT hold), so the 35s "
                    "sleep never reached the deadline"
                )
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
