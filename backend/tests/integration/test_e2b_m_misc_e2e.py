"""Broad-matrix real-SDK guardrails: misc contracts (E2B-M6..M10).

Part of the split E2B broad-matrix suite. These now run in the
standard paid integration leg (``run-integration-tests.sh``,
``make test-all``) whenever ``E2B_API_KEY`` is set and
``NO_INTEGRATION`` is unset — the suite was split across
``test_e2b_m_*_e2e.py`` siblings so ``--dist loadfile`` spreads the
cost across the xdist workers instead of running the whole sweep
serially on one worker.

This file gathers the lighter / skip-only members of the matrix so
they ride one worker without becoming a long-pole:

* M6 — read-only materialize failure on the python tier (one boot).
* M7 — documented no-op alias of E2B-T2 (skips, no compute).
* M8 — quota → ACCOUNT_LIMIT_EXCEEDED (skips, no compute).
* M9 — reconciler paused-snapshot branch (two tiny sandboxes).
* M10 — wipe_for_fresh_restart real end-to-end (one node session).

Cost: ~$0.03-0.05 across the file (M7/M8 are free skips). Every
sandbox is tagged with a per-run UUID and killed in a ``finally``
block (or torn down by ``service.session()``).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import StringIO

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.reconciler import E2BSandboxReconciler
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.sandbox_service import MaterializeFile
from tests.integration._e2b_broad_matrix_helpers import (
    E2B_API_KEY,
    IDLE_PAUSE_SECONDS,
    INITIALIZE_TIMEOUT,
    TEST_RUN_ID,
    is_template_missing_error,
    make_node_upstream,
    make_resources,
    make_service,
    make_test_client,
    make_test_metadata,
    make_uvx_time_upstream,
    sweep_kill,
)

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="needs a live E2B account (E2B_API_KEY unset)",
)


# ---------------------------------------------------------------------------
# E2B-M6 — materialize-file failure on a read-only path (python tier)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m6_materialize_readonly_path_fails_cleanly_broad_matrix() -> None:
    """E2B-M6: the read-only materialize-failure contract (E2B-T3) on
    the PYTHON tier with a uvx upstream. A write to ``/proc`` must
    surface as a clean connect failure, not a hang, regardless of the
    language template."""
    instance = f"e2e-m6-{TEST_RUN_ID}"
    client = make_test_client()
    service = make_service(instance=instance)
    upstream = make_uvx_time_upstream("m6")
    materialize = [
        MaterializeFile(
            name="READONLY_PROBE",
            target_path="/proc/mcpolis-m6-readonly.txt",
            contents=f"should-never-land-{TEST_RUN_ID}",
        ),
    ]
    errlog = StringIO()
    try:
        with pytest.raises(Exception) as exc_info:
            async with service.session(
                session_id=instance,
                org_id=f"acme-m6-{TEST_RUN_ID}",
                upstream=upstream,
                resources=make_resources(1.0, 1024),
                denylist=(),
                errlog=errlog,
                materialize_files=materialize,
            ):
                pass
        if is_template_missing_error(exc_info.value):
            pytest.skip(
                "mcpolis-python-cpu1-ram1024 not published — run the "
                "template grid build first.",
            )
        message = str(exc_info.value).lower()
        assert any(
            token in message
            for token in (
                "proc", "permission", "read-only", "readonly",
                "write", "denied", "no such", "materiali",
            )
        ), (
            "read-only materialize failure should surface a path/write "
            f"error on the python tier, got: {exc_info.value!r}"
        )
    finally:
        await sweep_kill(client, instance)


# ---------------------------------------------------------------------------
# E2B-M7 — sandbox killed mid-call → heal (alias of E2B-T2)
# ---------------------------------------------------------------------------


def test_e2b_m7_kill_mid_call_heal_is_covered_by_t2_broad_matrix() -> None:
    """E2B-M7: documented no-op alias.

    The spec offered "cover a distinct tier OR alias E2B-T2 with a
    note." The kill-mid-call→heal mechanism is provider-mechanism
    (transport_failed event + manager reconnect), not tier-sensitive:
    the SDK's kill + the manager's dead-transport reconnect behave
    identically regardless of CPU/RAM, so a second tier would spend
    money to re-prove the same code path with no new coverage.

    It is therefore covered once, deterministically, by
    ``test_e2b_targeted_recovery_e2e.py::
    test_t2_kill_mid_call_marks_transport_failed_then_heals`` (E2B-T2,
    node tier). This placeholder records that decision so the matrix
    stays legibly complete; it skips without spending compute."""
    pytest.skip(
        "E2B-M7 is aliased to E2B-T2 (test_e2b_targeted_recovery_e2e.py) — "
        "kill-mid-call→heal is tier-independent; see docstring.",
    )


# ---------------------------------------------------------------------------
# E2B-M8 — quota / rate-limit → ACCOUNT_LIMIT_EXCEEDED (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m8_quota_maps_to_account_limit_exceeded_broad_matrix() -> None:
    """E2B-M8: a provider quota / concurrent-sandbox cap must map to
    ``ExitReason.ACCOUNT_LIMIT_EXCEEDED`` via ``map_exit`` against the
    real SDK error shape.

    Hitting the cap deterministically is NOT possible from a test: it
    depends on the account's concurrent-sandbox limit (Pro tiers allow
    dozens), and spawning enough sandboxes to trip it would cost real
    money AND could wedge a shared CI account for other runs. We
    therefore skip with an operator note rather than write a flaky
    assert or a runaway-cost spawner.

    OPERATOR REPRO: to exercise this path against a live account,
    temporarily lower the account's max-concurrent-sandbox limit in the
    E2B dashboard to a small N, then spawn N+1 sandboxes via
    ``RealE2BClient.create_sandbox`` in a loop; the N+1th raises the
    SDK's rate-limit/quota exception, which ``RealE2BClient`` wraps as
    ``E2BQuotaError`` and ``E2BSandboxService.map_exit`` translates to
    ``ExitReason.ACCOUNT_LIMIT_EXCEEDED``. The wrapping + mapping logic
    itself is unit-tested against the typed ``E2BQuotaError`` in the
    mock-driven service suite; this integration leg would only add
    confidence that the LIVE SDK exception class is the one the wrapper
    recognizes."""
    pytest.skip(
        "E2B-M8: quota/rate-limit is not deterministically inducible "
        "without lowering the account cap + a paid spawn loop; see "
        "docstring for the operator repro. The E2BQuotaError → "
        "ACCOUNT_LIMIT_EXCEEDED mapping is unit-tested against the typed "
        "error in the mock service suite.",
    )


# ---------------------------------------------------------------------------
# E2B-M9 — reconciler real end-to-end with paused-snapshot coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m9_reconciler_keeps_paused_snapshot_broad_matrix() -> None:
    """E2B-M9: reconciler end-to-end, DISTINCT from E2B-T4 by covering
    the PAUSED-snapshot branch (T4 covers the running-orphan kill).

    Setup: one running orphan (unpersisted) + one paused snapshot whose
    id IS persisted as ``paused_snapshot_id`` (recognized). The
    reconciler must kill the running orphan AND keep the recognized
    paused snapshot (``kept_paused_snapshots`` counts it), exercising
    the ``state == 'paused'`` + ``recognized_paused`` arm of
    ``reconcile()`` that T4 never reaches."""
    client = make_test_client()
    instance = f"e2e-m9-{TEST_RUN_ID}"
    persistence = InMemorySandboxPersistenceRepository()
    org_id = f"acme-m9-{TEST_RUN_ID}"

    orphan_id: str | None = None
    paused_id: str | None = None
    try:
        # Running orphan — unpersisted, must be killed.
        orphan = await client.create_sandbox(
            template="base",
            metadata={"mcpolis_instance": instance, **make_test_metadata("m9-orphan")},
            timeout_seconds=120,
        )
        orphan_id = orphan.sandbox_id

        # A sandbox we pause → its snapshot id is what we recognize.
        to_pause = await client.create_sandbox(
            template="base",
            metadata={"mcpolis_instance": instance, **make_test_metadata("m9-paused")},
            timeout_seconds=120,
        )
        paused_id = await to_pause.pause()
        assert paused_id, "pause returned an empty snapshot id"

        # Persist the paused snapshot as recognized.
        await persistence.upsert(SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=f"e2e-m9-{TEST_RUN_ID}",
            mcpolis_instance=instance,
            sandbox_id=None,
            paused_snapshot_id=paused_id,
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

        assert report.killed_orphan_sandboxes >= 1, (
            f"running orphan should be killed; report={report!r}"
        )
        assert report.kept_paused_snapshots >= 1, (
            "the recognized paused snapshot must be KEPT (not GC'd); "
            f"report={report!r}"
        )

        # Provider view: the running orphan is gone from the listing.
        remaining = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": instance},
        )
        remaining_ids = {info.sandbox_id for info in remaining}
        assert orphan_id not in remaining_ids, (
            "running orphan must be gone after reconcile"
        )
        # The recognized paused snapshot must SURVIVE the reconcile. Verify
        # with the same round-trip the real-SDK pause/resume test uses
        # (connect_sandbox accepts the snapshot id and returns a handle to
        # the same underlying sandbox) rather than assuming the snapshot id
        # appears verbatim in list_sandboxes() — a paused sandbox is not
        # guaranteed to be listed under that id.
        resumed = await client.connect_sandbox(paused_id)
        assert resumed.sandbox_id == paused_id
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip("base/templates unavailable on the active account")
        raise
    finally:
        for sandbox_id in (orphan_id, paused_id):
            if sandbox_id is not None:
                try:
                    await client.kill_sandbox(sandbox_id)
                except E2BSDKError:
                    pass


# ---------------------------------------------------------------------------
# E2B-M10 — wipe_for_fresh_restart / orphan-reaper real end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2b_m10_wipe_for_fresh_restart_kills_and_clears_broad_matrix() -> None:
    """E2B-M10: promote ``e2b_real_e2e.py::restart_with_fresh``'s wipe
    assertion into a real pytest. ``wipe_for_fresh_restart`` must kill
    the persisted live sandbox AND clear its persistence ref, so the
    next reconnect fresh-creates from a clean slate.

    A real session is opened against the node template, preserved on
    close (so the sandbox + ref survive the session exit under the
    kill-on-stop contract), then wiped. Asserts: wipe returns 1, the
    ref is gone, and the original sandbox is no longer present on E2B."""
    instance = f"e2e-m10-{TEST_RUN_ID}"
    org_id = f"acme-m10-{TEST_RUN_ID}"
    upstream = make_node_upstream("m10")
    persistence = InMemorySandboxPersistenceRepository()
    client = make_test_client()
    assert E2B_API_KEY is not None
    service = E2BSandboxService(
        client,
        mcpolis_instance=instance,
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
        persistence=persistence,
        reuse_sandboxes_on_restart=True,
    )
    original_sandbox_id: str | None = None
    errlog = StringIO()
    try:
        session_id = f"{instance}-a"
        async with service.session(
            session_id=session_id,
            org_id=org_id,
            upstream=upstream,
            resources=make_resources(1.0, 1024),
            denylist=(),
            errlog=errlog,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
            # Preserve so the wipe has a live ref + sandbox to act on
            # (the kill-on-stop default would otherwise delete both
            # before the wipe asserts on them).
            service.mark_session_preserve_on_close(session_id)

        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert ref is not None and ref.sandbox_id is not None, (
            "session should have persisted a live ref"
        )
        original_sandbox_id = ref.sandbox_id

        cleared = await service.wipe_for_fresh_restart()
        assert cleared == 1, f"wipe should clear exactly 1 ref, got {cleared}"

        after = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        assert after is None, "persistence ref must be gone after wipe"

        # The original sandbox must no longer be present on E2B.
        remaining = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": instance},
        )
        remaining_ids = {info.sandbox_id for info in remaining}
        assert original_sandbox_id not in remaining_ids, (
            "wipe_for_fresh_restart must kill the persisted sandbox on E2B"
        )
        original_sandbox_id = None  # killed by wipe; nothing to clean up
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-node-cpu1-ram1024 not published — run the "
                "template grid build first.",
            )
        tail = errlog.getvalue()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise
    finally:
        if original_sandbox_id is not None:
            try:
                await client.kill_sandbox(original_sandbox_id)
            except E2BSDKError:
                pass
        await sweep_kill(client, instance)
