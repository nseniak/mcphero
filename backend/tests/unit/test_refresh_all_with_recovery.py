"""Admin-MCP refresh stall recovery (R6).

``refresh_upstream_tools`` used a raw ``refresh_upstream`` / ``refresh_all``
with no recovery. Both branches now route through
``acquire_and_refresh_with_recovery``, which is identity-coupled (one
``effective_user``). The subtlety: admin discovery is identity-AGNOSTIC —
it reuses any user's live session — so the recovery must heal under the
user who actually OWNS the session, not the calling admin.
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import DiscoveredTool, UpstreamDefinition
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.services import upstream_connection_service as ucs_module
from mcpolis.domain.services.upstream_connection_service import (
    recovery_effective_user,
    refresh_all_with_recovery,
)
from tests.unit._state_seed import seed_user_session
from tests.unit.factories import make_upstream_auth, make_upstream_definition


def make_tool(upstream_id: str) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name="echo",
        prefixed_name=f"{upstream_id}__echo",
        description="echo",
        input_schema={},
    )


# --- recovery_effective_user --------------------------------------------------


def test_recovery_effective_user_service_account_is_empty() -> None:
    upstream = make_upstream_definition(id="sa")  # service_account
    cm = UpstreamClientManager([upstream])
    assert recovery_effective_user(cm, upstream) == ""


def test_recovery_effective_user_oauth_uses_session_owner() -> None:
    """R6: ``_ensure_oauth_session`` may have reused ANOTHER user's session.
    The recovery must target that owner so it reconnects from the right
    stored tokens — not the calling admin."""
    upstream = make_upstream_definition(
        id="notion", auth=make_upstream_auth(mode=AuthMode.per_user_oauth),
    )
    cm = UpstreamClientManager([upstream])
    seed_user_session(cm, "notion", "bob@co.com", session=cast(Any, object()))

    assert recovery_effective_user(cm, upstream) == "bob@co.com"


def test_recovery_effective_user_oauth_no_session_falls_back_to_empty() -> None:
    upstream = make_upstream_definition(
        id="notion", auth=make_upstream_auth(mode=AuthMode.per_user_oauth),
    )
    cm = UpstreamClientManager([upstream])
    assert recovery_effective_user(cm, upstream) == ""


# --- refresh_all_with_recovery ------------------------------------------------


class _FakeManager:
    """The slice of ``UpstreamClientManager`` ``refresh_all_with_recovery``
    + ``recovery_effective_user`` touch: connected ids, upstream lookup,
    session-owner lookup."""

    def __init__(
        self,
        connected: list[str],
        upstreams: dict[str, UpstreamDefinition],
        owners: dict[str, str],
    ) -> None:
        self._connected = connected
        self._upstreams = upstreams
        self._owners = owners

    @property
    def connected_upstream_ids(self) -> list[str]:
        return list(self._connected)

    def get_upstream(self, upstream_id: str) -> UpstreamDefinition | None:
        return self._upstreams.get(upstream_id)

    def first_user_with_session(self, upstream_id: str) -> str | None:
        return self._owners.get(upstream_id)


@pytest.mark.asyncio
async def test_refresh_all_routes_each_under_right_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each connected upstream is refreshed-with-recovery under the right
    ``effective_user``: ``""`` for service_account, the session OWNER for
    OAuth. One upstream failing must NOT abort the sweep (mirrors
    ``ToolRegistry.refresh_all``'s per-upstream tolerance)."""
    sa = make_upstream_definition(id="sa")  # service_account
    oauth = make_upstream_definition(
        id="notion", auth=make_upstream_auth(mode=AuthMode.per_user_oauth),
    )
    cm = _FakeManager(
        connected=["sa", "notion"],
        upstreams={"sa": sa, "notion": oauth},
        owners={"notion": "bob@co.com"},
    )

    calls: list[tuple[str, str]] = []

    async def fake_recovery(
        *, upstream: UpstreamDefinition, effective_user: str, **_: Any,
    ) -> list[DiscoveredTool]:
        calls.append((upstream.id, effective_user))
        if upstream.id == "sa":
            raise RuntimeError("boom")  # one upstream fails
        return [make_tool(upstream.id)]

    monkeypatch.setattr(
        ucs_module, "acquire_and_refresh_with_recovery", fake_recovery,
    )

    await refresh_all_with_recovery(
        org_id="acme",
        connection_store=None,
        client_manager=cast(Any, cm),
        tool_registry=cast(Any, object()),
        server_url="http://localhost:8000",
    )

    assert ("sa", "") in calls, "service_account heals under the shared session"
    assert ("notion", "bob@co.com") in calls, (
        "OAuth heals under the session OWNER, not the calling admin"
    )
    assert len(calls) == 2, "a failing upstream must not abort the sweep"


# --- CONC-1: refresh_all_with_recovery runs the per-upstream refreshes
#     CONCURRENTLY, not serially ----------------------------------------------


@pytest.mark.asyncio
async def test_refresh_all_runs_upstreams_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONC-1: ``refresh_all_with_recovery`` gathers the per-upstream
    refreshes so the admin-MCP caller blocks for the SLOWEST single
    upstream, not the SUM (the ``asyncio.gather`` at
    upstream_connection_service.py:1684).

    Determinism: each upstream's refresh signals an
    ``asyncio.Event`` on entry and then awaits a shared release gate. If
    the sweep ran serially the SECOND upstream would never enter while
    the first is parked, so the "both entered" wait would hang — the
    overlap is proven by both entry-events being set BEFORE either
    refresh is allowed to complete, with NO real sleep anywhere.
    """
    a = make_upstream_definition(id="a")  # service_account
    b = make_upstream_definition(id="b")  # service_account
    cm = _FakeManager(
        connected=["a", "b"],
        upstreams={"a": a, "b": b},
        owners={},
    )

    entered: dict[str, asyncio.Event] = {
        "a": asyncio.Event(),
        "b": asyncio.Event(),
    }
    release = asyncio.Event()
    completed: list[str] = []

    async def gated_recovery(
        *, upstream: UpstreamDefinition, **_: Any,
    ) -> list[DiscoveredTool]:
        entered[upstream.id].set()
        # Park here holding the "slot" — a serial sweep could not start
        # the second upstream until this returns.
        await release.wait()
        completed.append(upstream.id)
        return [make_tool(upstream.id)]

    monkeypatch.setattr(
        ucs_module, "acquire_and_refresh_with_recovery", gated_recovery,
    )

    sweep = asyncio.create_task(
        refresh_all_with_recovery(
            org_id="acme",
            connection_store=None,
            client_manager=cast(Any, cm),
            tool_registry=cast(Any, object()),
            server_url="http://localhost:8000",
        )
    )

    # Both upstreams must be in-flight (overlapping) before either is
    # allowed to finish. If the sweep were serial this gather would never
    # resolve — the second event stays clear while the first is parked.
    await asyncio.wait_for(
        asyncio.gather(entered["a"].wait(), entered["b"].wait()),
        timeout=5.0,
    )
    assert not completed, (
        "neither refresh may complete before both have entered — proves "
        "they overlap rather than running one-after-the-other"
    )

    release.set()
    await asyncio.wait_for(sweep, timeout=5.0)
    assert sorted(completed) == ["a", "b"], "both refreshes complete"


@pytest.mark.asyncio
async def test_refresh_all_one_failure_does_not_abort_concurrent_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONC-1: a single upstream RAISING mid-sweep must not cancel the
    sibling refresh that is concurrently in-flight (the
    ``return_exceptions=True`` belt-and-braces on the gather). The
    failing upstream is released first and raises; the slow one is still
    parked, then released, and must still complete.
    """
    ok = make_upstream_definition(id="ok")
    bad = make_upstream_definition(id="bad")
    cm = _FakeManager(
        connected=["ok", "bad"],
        upstreams={"ok": ok, "bad": bad},
        owners={},
    )

    entered: dict[str, asyncio.Event] = {
        "ok": asyncio.Event(),
        "bad": asyncio.Event(),
    }
    release_ok = asyncio.Event()
    completed: list[str] = []

    async def gated_recovery(
        *, upstream: UpstreamDefinition, **_: Any,
    ) -> list[DiscoveredTool]:
        entered[upstream.id].set()
        if upstream.id == "bad":
            # Fail immediately, while ``ok`` is still parked in-flight.
            raise RuntimeError("boom")
        await release_ok.wait()
        completed.append("ok")
        return [make_tool("ok")]

    monkeypatch.setattr(
        ucs_module, "acquire_and_refresh_with_recovery", gated_recovery,
    )

    sweep = asyncio.create_task(
        refresh_all_with_recovery(
            org_id="acme",
            connection_store=None,
            client_manager=cast(Any, cm),
            tool_registry=cast(Any, object()),
            server_url="http://localhost:8000",
        )
    )

    # Wait until both have entered: ``bad`` raises right away, ``ok`` is
    # parked. The failure must NOT cancel ``ok`` mid-flight.
    await asyncio.wait_for(
        asyncio.gather(entered["ok"].wait(), entered["bad"].wait()),
        timeout=5.0,
    )
    release_ok.set()
    await asyncio.wait_for(sweep, timeout=5.0)
    assert completed == ["ok"], (
        "the surviving upstream completes even though its sibling raised"
    )
