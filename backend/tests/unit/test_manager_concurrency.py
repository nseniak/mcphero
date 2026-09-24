"""Manager-level concurrency guardrails for the shared service_account
session lifecycle, driven by a REAL in-memory MCP server.

These build on the new ``FakeSandboxService`` (``make_fake_sandbox_service``)
whose ``session()`` runs a genuine low-level MCP server over memory streams,
so ``connect_shared`` completes a real ``initialize`` handshake and
``session_open_count`` is a faithful "how many sandboxes did we actually
open?" oracle. The deterministic choke point is a FastMCP ``lifespan`` gated
on an ``asyncio.Event``: the low-level server enters the lifespan BEFORE it
answers ``initialize``, so a gated lifespan holds ``connect_shared``
in-flight with no real sleep — letting concurrent callers pile onto the
single-flight while the first connect is provably still running.

Distinct from ``test_reconnect_shared_fresh_single_flight.py`` (which
stubs the reopen body) and the ``_FakeTask`` cross-pool
race in ``test_ensure_shared_connected_heals_dead_session.py``: here the
session is a real ``ClientSession`` and the coalesced callers go on to make
a real ``call_tool`` on the healed session — the end-to-end "single-flight
coalesces AND the survivors land on a usable transport" contract.

NOTE: no ``from __future__ import annotations`` — FastMCP tool registration
calls ``issubclass()`` on annotations, which breaks under stringified
annotations (see the same note in ``fake_sandbox_service.py``).
"""
import asyncio
import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import structlog
from mcp.server.fastmcp import FastMCP

from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.session_single_flight import (
    ConnectAborted,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
)
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import (
    ServerInfo,
    ToolAnnotations,
    UpstreamDefinition,
    UpstreamSelfDescription,
)
from mcpolis.domain.services import tool_router as tool_router_module
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import (
    ToolRouter,
    dispatch_with_liveness,
)
from mcpolis.domain.services.upstream_connection_service import (
    heal_stalled_session,
)
from tests.unit._shared_session_harness import (
    make_manager,
    make_gated_server_factory,
    wait_until,
)
from tests.unit.factories import make_discovered_tool, make_upstream_definition
from tests.unit.fake_sandbox_service import make_fake_sandbox_service




def make_echo_server() -> FastMCP:
    server = FastMCP(name="ConcUpstream")

    @server.tool(name="echo", description="Echo back the message")
    def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"echo:{message}"

    return server






# --- CONC-2: concurrent reconnect_shared_fresh coalesces to ONE session,
#     and every caller lands on the healed, usable session -----------------


@pytest.mark.asyncio
async def test_concurrent_reconnect_coalesces_to_one_real_session() -> None:
    """CONC-2: N concurrent ``reconnect_shared_fresh`` while the first is
    held in-flight open exactly ONE new sandbox (single-flight coalesces),
    and after the heal every caller can ``call_tool`` on the fresh session.

    The first reconnect's reopen is parked inside the gated lifespan; the
    other N-1 join it. A missing single-flight would show as
    ``session_open_count`` > 1 (N sandboxes, N-1 orphaned)."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    # Kick off the first heal; it opens a session and parks in the lifespan.
    first = asyncio.create_task(mgr.reconnect_shared_fresh(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)
    assert not first.done(), "first reconnect is held in-flight by the gate"

    # Siblings arrive while the first is still connecting.
    siblings = [
        asyncio.create_task(mgr.reconnect_shared_fresh(upstream))
        for _ in range(7)
    ]
    # Give them a turn to reach the single-flight join point.
    await asyncio.sleep(0)
    assert fake.session_open_count == 1, (
        "no second sandbox may open while the first reconnect is in-flight"
    )

    gate.set()
    await asyncio.wait_for(asyncio.gather(first, *siblings), timeout=5.0)

    assert fake.session_open_count == 1, (
        "8 concurrent healers must coalesce onto ONE fresh session, not "
        "open 8 sandboxes (7 orphaned)"
    )

    # Every caller now shares the one healed session, which must be usable.
    session = mgr.get_session("everything2")
    result = await session.call_tool("echo", {"message": "healed"})
    assert not result.isError
    assert "echo:healed" in result.content[0].text  # type: ignore[union-attr]

    await mgr.stop_all()


@pytest.mark.asyncio
async def test_sequential_reconnect_opens_a_fresh_session_each_time() -> None:
    """CONC-2 (counterpart): the single-flight coalesces only TRULY
    concurrent healers — a heal that arrives AFTER a prior one completed
    reflects a genuinely later stall and must open its own fresh session.
    Pins that the coalescing window doesn't over-reach into "reuse the old
    sandbox forever"."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    await mgr.reconnect_shared_fresh(upstream)
    assert fake.session_open_count == 1
    await mgr.reconnect_shared_fresh(upstream)
    assert fake.session_open_count == 2, (
        "a heal after the prior reconnect completed forces its own fresh "
        "session"
    )

    session = mgr.get_session("everything2")
    result = await session.call_tool("echo", {"message": "x"})
    assert not result.isError

    await mgr.stop_all()


@pytest.mark.asyncio
async def test_heal_asks_to_preserve_the_sandbox_before_reopening() -> None:
    """A heal must keep the warm sandbox, and asking is not optional.

    ``reconnect_shared_fresh`` leaves the persisted ref in place so the
    reopen can reuse the sandbox. That alone does nothing: the reopen
    is close-then-open, and the close tears the session down with
    ``preserve=False``, which deletes the ref and kills the sandbox.
    The reopen then has nothing to reuse and fresh-creates — 7-22s of
    package download on every single wake, the opposite of the
    intent.

    An independent review caught exactly that: the code claimed reuse,
    logged ``sandbox_reused=True``, and destroyed the sandbox anyway.
    The only reason the integration tests appeared to show reuse was
    that they called ``mark_session_preserve_on_close`` by hand, which
    production never does. This test is the regression gate.
    """
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    # A live session first, so "before the reopen" is a real
    # distinction rather than trivially true on an empty manager.
    await mgr.connect_shared(upstream)
    fake.preserve_calls.clear()
    fake.opens_at_preserve.clear()
    fake.unclosed_at_preserve.clear()

    await mgr.reconnect_shared_fresh(upstream)
    assert fake.preserve_calls, (
        "the heal must ask the sandbox service to preserve the sandbox; "
        "without it every wake pays a full cold create"
    )
    assert fake.preserve_calls[-1][1] == "everything2", (
        f"preserve must be scoped to this upstream, not a blanket mark; "
        f"got {fake.preserve_calls}"
    )
    # ORDER, not just occurrence. Asking after the reopen is useless:
    # the close has already deleted the ref and killed the sandbox.
    # Review showed the occurrence-only version of this assertion
    # passed with the call moved after ``connect_shared``, so it
    # guarded nothing. ``opens_at_preserve`` is the session-open count
    # sampled inside the preserve call; it must still be pre-heal.
    assert fake.opens_at_preserve == [1], (
        "preserve must be asked for BEFORE the reopen; session opens "
        f"at preserve time = {fake.opens_at_preserve}"
    )
    # And before the CLOSE, which is what deletes the ref and kills the
    # sandbox. "Before the reopen" alone let a preserve moved after the
    # close pass.
    assert fake.unclosed_at_preserve == [1], (
        "preserve must be asked for while the old session is still open; "
        f"unclosed sessions at preserve time = {fake.unclosed_at_preserve}"
    )

    await mgr.stop_all()


@pytest.mark.asyncio
async def test_lazy_reattach_after_a_wake_also_preserves_the_sandbox() -> None:
    """The wake path preserves the sandbox too, not just the heal path.

    This guard was missing, and its absence is how one bug reached
    review twice by two different routes. The first design healed
    through ``reconnect_shared_fresh``, so the preserve call went
    there. The second design detects the pause in the watcher, so the
    next dispatch resolves through ``ensure_shared_connected``
    instead — which closed and rebuilt the sandbox with no preserve,
    silently undoing the whole saving while the heal-path test stayed
    green.

    Every close-then-open of a live shared session must preserve,
    whichever entry point reached it.
    """
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    await mgr.connect_shared(upstream)
    await wait_until(lambda: fake.last_session is not None)
    handle = fake.last_session
    assert handle is not None

    # The sandbox pauses: the backend marks the transport dead on its
    # own, exactly as the E2B watcher now does.
    handle.kill()
    fake.preserve_calls.clear()
    fake.opens_at_preserve.clear()
    fake.unclosed_at_preserve.clear()

    # The next dispatch resolves its session through here.
    await mgr.ensure_shared_connected(upstream)

    assert fake.preserve_calls, (
        "waking through the lazy-attach path must also ask to keep the "
        "sandbox; otherwise every idle period ends in a full package "
        "download"
    )
    assert fake.preserve_calls[-1][1] == "everything2"
    # Order, same as the heal twin. The preserve lives inside
    # ``connect_shared`` so one assertion technically covers both, but
    # this is the path carrying production traffic and the one whose
    # guard was missing last round.
    assert fake.opens_at_preserve == [1], (
        "preserve must be asked for BEFORE the reopen; session opens "
        f"at preserve time = {fake.opens_at_preserve}"
    )
    assert fake.unclosed_at_preserve == [1], (
        "preserve must be asked for while the dead session is still "
        "open; closing it first deletes the ref and kills the sandbox"
    )

    await mgr.stop_all()


# --- CONC-3: a lazy-connect failure under contention clears the slot and
#     fails BOTH callers, leaving the upstream FAILED ----------------------


@pytest.mark.asyncio
async def test_lazy_connect_failure_under_contention_clears_slot() -> None:
    """CONC-3: when the in-flight ``connect_shared`` fails, two concurrent
    ``ensure_shared_connected`` callers must BOTH observe the failure, the
    failed connect must not linger as something to join (so the next
    dispatch retries rather than awaiting a dead task), and the upstream
    must land FAILED.

    The failure is driven deterministically by ``fire_exit`` on the live
    session handle: it resolves the per-session ExitSignal so the
    ``initialize`` race in ``init_with_exit_race`` loses to the exit branch
    and raises ``SubprocessExitedDuringInit`` FAST — no 120s
    init-timeout wait. Both callers share the single in-flight connect
    (``session_open_count == 1``), so both inherit its failure."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    c1 = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    c2 = asyncio.create_task(mgr.ensure_shared_connected(upstream))

    # One session opens (single-flight); grab its handle and kill its
    # subprocess mid-init so the connect fails fast for BOTH callers.
    await wait_until(lambda: fake.last_session is not None)
    assert fake.session_open_count == 1, (
        "both lazy callers must coalesce onto ONE connect attempt"
    )
    handle = fake.last_session
    assert handle is not None
    handle.fire_exit(exit_code=1, stderr_tail="boot crash")

    results = await asyncio.wait_for(
        asyncio.gather(c1, c2, return_exceptions=True), timeout=5.0,
    )
    assert all(isinstance(r, Exception) for r in results), (
        "both contending callers observe the connect failure"
    )

    state = mgr.get_state("everything2")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED, (
        "a failed lazy attach must mark the upstream FAILED"
    )

    # The failed connect must not linger as something to join: the next
    # dispatch starts a fresh attempt, and that one works.
    assert not mgr._shared_flights.in_flight("everything2")  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(mgr.ensure_shared_connected(upstream), timeout=5.0)
    assert fake.session_open_count == 2, (
        "the retry must open its own session, not inherit the dead attempt"
    )
    await echo_through(mgr, "everything2")

    await mgr.stop_all()


# --- One shared connect per upstream, whichever entry point asks -----------
#
# ``connect_shared`` is the one place every shared-session connect goes
# through: the lazy attach on a tool call, the dashboard's Start, the boot
# connect, the heal. Only two of those used to coalesce, through locks
# held at their own call sites. The rest opened a second sandbox for the
# same upstream whenever they overlapped with another connect, and the
# loser's teardown could delete the winner's sandbox record. These tests
# race the entry points against each other and count sandbox opens.


async def echo_through(mgr: UpstreamClientManager, upstream_id: str) -> None:
    session = mgr.get_session(upstream_id)
    result = await asyncio.wait_for(
        session.call_tool("echo", {"message": "ok"}), timeout=5.0,
    )
    assert not result.isError
    assert "echo:ok" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_dashboard_start_joins_a_lazy_attach_in_flight() -> None:
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    lazy = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)
    start = asyncio.create_task(mgr.connect_upstream(upstream))
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(lazy, start), timeout=5.0)

    assert fake.session_open_count == 1, (
        "Start must join the connect already in flight, not open a second "
        "sandbox for the same upstream"
    )
    await echo_through(mgr, "everything2")
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_lazy_attach_joins_a_dashboard_start_in_flight() -> None:
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    start = asyncio.create_task(mgr.connect_upstream(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)
    lazy = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(start, lazy), timeout=5.0)

    assert fake.session_open_count == 1, (
        "a tool call arriving during Start must use Start's connect"
    )
    await echo_through(mgr, "everything2")
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_boot_connect_and_a_lazy_attach_share_one_connect() -> None:
    """Right after a deploy, the boot connect runs while the first tool
    calls arrive. Either order must end with one sandbox."""
    for boot_first in (True, False):
        gate = asyncio.Event()
        fake = make_fake_sandbox_service(
            server_factory=make_gated_server_factory(gate),
        )
        upstream = make_upstream_definition(id="everything2", command="ignored")
        mgr = make_manager(upstream, fake)

        boot: asyncio.Task[Any]
        lazy: asyncio.Task[Any]
        if boot_first:
            boot = asyncio.create_task(mgr.connect_shared_or_defer(upstream))
            await wait_until(lambda: fake.session_open_count >= 1)
            lazy = asyncio.create_task(mgr.ensure_shared_connected(upstream))
        else:
            lazy = asyncio.create_task(mgr.ensure_shared_connected(upstream))
            await wait_until(lambda: fake.session_open_count >= 1)
            boot = asyncio.create_task(mgr.connect_shared_or_defer(upstream))
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(boot, lazy), timeout=5.0)

        assert fake.session_open_count == 1, (
            f"boot_first={boot_first}: boot and a tool call opened "
            f"{fake.session_open_count} sandboxes for one upstream"
        )
        await echo_through(mgr, "everything2")
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_preserve_is_asked_once_per_reopen_under_contention() -> None:
    """Heals, a boot-style connect, a Start and a lazy attach all ask for
    the same upstream while one reopen is in flight. That is ONE reopen:
    one request to keep the warm sandbox, made before the reopen, and one
    new session. A second reopen would ask again and replace the process
    the first one just started."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(
            gate, hold=lambda index: index == 2,
        ),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    await mgr.connect_shared(upstream)
    fake.preserve_calls.clear()
    fake.opens_at_preserve.clear()
    fake.unclosed_at_preserve.clear()

    heals = [
        asyncio.create_task(mgr.reconnect_shared_fresh(upstream))
        for _ in range(3)
    ]
    await wait_until(lambda: fake.session_open_count >= 2)
    others = [
        asyncio.create_task(mgr.connect_shared(upstream)),
        asyncio.create_task(mgr.connect_upstream(upstream)),
        asyncio.create_task(mgr.ensure_shared_connected(upstream)),
    ]
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(*heals, *others), timeout=5.0)

    assert fake.preserve_calls == [("default", "everything2")], (
        f"one reopen asks to keep the sandbox once; got {fake.preserve_calls}"
    )
    assert fake.opens_at_preserve == [1], (
        "the one preserve request must come BEFORE the reopen"
    )
    assert fake.unclosed_at_preserve == [1], (
        "the one preserve request must come BEFORE the old session closes"
    )
    assert fake.session_open_count == 2, (
        "everyone who asked during the reopen must share it"
    )
    await echo_through(mgr, "everything2")
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_no_connect_entry_point_deadlocks_on_the_same_upstream() -> None:
    """Every entry point, one after another and then all at once, with a
    hard ceiling. The lazy attach and the heal used to call
    ``connect_shared`` while holding a per-upstream lock; putting a lock
    inside ``connect_shared`` as well would hang them forever, because an
    ``asyncio.Lock`` cannot be taken twice by the same task."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    async def every_entry_point() -> None:
        await mgr.ensure_shared_connected(upstream)
        await mgr.reconnect_shared_fresh(upstream)
        await mgr.connect_shared(upstream)
        await mgr.connect_upstream(upstream)
        await mgr.connect_shared_or_defer(upstream)
        await asyncio.gather(
            mgr.ensure_shared_connected(upstream),
            mgr.reconnect_shared_fresh(upstream),
            mgr.connect_shared(upstream),
            mgr.connect_upstream(upstream),
            mgr.connect_shared_or_defer(upstream),
        )

    await asyncio.wait_for(every_entry_point(), timeout=10.0)
    await echo_through(mgr, "everything2")
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_cancelling_the_lazy_attach_initiator_does_not_fail_its_waiters() -> None:
    """The tool call that started a lazy attach goes away (its client hung
    up). Another call is waiting on the same attach. The waiter must get
    the session, not the first caller's cancellation."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    first = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)
    second = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    for _ in range(20):
        await asyncio.sleep(0)
    first.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()

    await asyncio.wait_for(second, timeout=5.0)
    with pytest.raises(asyncio.CancelledError):
        await first
    assert fake.session_open_count == 1
    await echo_through(mgr, "everything2")
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_lone_caller_that_gives_up_stops_the_connect() -> None:
    """Nobody else is waiting, so giving up stops the connect, as it did
    before connects were shared: no session appears afterwards."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    lone = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)
    lone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lone
    gate.set()
    await asyncio.sleep(0.2)  # room for an un-stopped connect to land

    state = mgr.get_state("everything2")
    assert state is not None
    assert state.shared_session is None, (
        "a connect nobody waits for must not install a session later"
    )
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_stop_stops_a_connect_that_a_tool_call_joined() -> None:
    """Stop during Start, while a tool call waits on the same upstream.
    After Stop the upstream must stay stopped: no connect still running
    for the tool call may bring it back to life a moment later."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate, hold=lambda _: True),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    # The dashboard's Start: a background connect the manager tracks.
    async def start_connect() -> None:
        await mgr.connect_upstream(upstream)

    start = asyncio.create_task(start_connect())
    mgr.register_background_connect_task("everything2", start)
    await wait_until(lambda: fake.session_open_count >= 1)
    tool_call = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    for _ in range(20):
        await asyncio.sleep(0)

    await asyncio.wait_for(mgr.disconnect_upstream("everything2"), timeout=5.0)
    gate.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(start, tool_call, return_exceptions=True), timeout=5.0,
    )
    await asyncio.sleep(0.2)  # room for an un-stopped connect to land

    assert all(isinstance(o, BaseException) for o in outcomes), (
        f"neither caller may get a session after Stop; got {outcomes!r}"
    )
    assert isinstance(outcomes[0], asyncio.CancelledError), (
        "Start must read as cancelled (the admin stopped it), not as a "
        f"failure that paints an error on the dashboard; got {outcomes[0]!r}"
    )
    state = mgr.get_state("everything2")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    assert state.shared_session is None
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_heal_that_lost_the_race_does_not_reopen_the_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call stalls on the shared session. Before its heal runs,
    another heal has already reopened the session. The late heal must use
    the fresh session, not reopen it again: that would replace the process
    the other callers just moved onto, mid-call."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    registry = ToolRegistry([upstream], mgr)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(
            upstream_id="everything2",
            original_name="echo",
            annotations=ToolAnnotations(idempotentHint=True),
        ),
    ]
    router = ToolRouter(
        registry, mgr, FileAuditRepository(tmp_path / "audit.jsonl"),
        [upstream], policy_engine=PolicyEngine(SettingsConfig()),
    )
    # Detect the stall in a tenth of a second instead of 40.
    monkeypatch.setattr(
        tool_router_module, "dispatch_with_liveness",
        functools.partial(
            dispatch_with_liveness, probe_interval=0.05, ping_timeout=0.05,
        ),
    )

    async def heal_after_a_rival_heal(**kwargs: Any) -> None:
        await mgr.reconnect_shared_fresh(upstream)
        await heal_stalled_session(**kwargs)

    monkeypatch.setattr(
        tool_router_module, "heal_stalled_session", heal_after_a_rival_heal,
    )

    await mgr.connect_shared(upstream)
    handle = fake.last_session
    assert handle is not None
    handle.stall()

    result = await asyncio.wait_for(
        router.route_call(
            org_id="default",
            prefixed_name="everything2__echo",
            arguments={"message": "x"},
            user_id="alice@co.com",
            session_id=None,
        ),
        timeout=30.0,
    )
    assert not result.isError, result
    assert fake.session_open_count == 2, (
        "the stalled session was reopened once by the rival heal; the late "
        f"heal reopened it again ({fake.session_open_count} opens)"
    )
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_connect_that_waits_on_itself_fails_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a connect ever re-enters its own upstream (say, a callback fired
    during ``initialize`` asks for the session), it would wait on itself
    forever. It must fail loudly and at once instead, and leave nothing
    behind that later callers would join."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    real_create_task = mgr._create_task  # pyright: ignore[reportPrivateUsage]
    re_entered = False

    async def create_task_that_re_enters(*args: Any, **kwargs: Any) -> Any:
        nonlocal re_entered
        if not re_entered:
            re_entered = True
            await mgr.ensure_shared_connected(upstream)
        return await real_create_task(*args, **kwargs)

    monkeypatch.setattr(mgr, "_create_task", create_task_that_re_enters)

    with pytest.raises(RuntimeError, match="waited on itself"):
        await asyncio.wait_for(mgr.ensure_shared_connected(upstream), timeout=5.0)

    # The next caller starts clean and succeeds.
    await asyncio.wait_for(mgr.ensure_shared_connected(upstream), timeout=5.0)
    await echo_through(mgr, "everything2")
    await mgr.stop_all()


# --- Failure handling does not depend on who started the connect ----------
#
# Second review: the entry point that STARTED a shared connect decided what
# happened when it failed, so callers who joined from another entry point
# got handling written for someone else. These pin the outcome per caller.


def make_failing_create_task(
    entered: asyncio.Event, release: asyncio.Event, message: str,
) -> Callable[..., Any]:
    """Fault injection for the connect itself: it signals ``entered``,
    waits for ``release``, then fails with ``message``."""

    async def create_task(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        raise RuntimeError(message)

    return create_task


def make_start(
    mgr: UpstreamClientManager, upstream: UpstreamDefinition,
    recorded_errors: list[str],
) -> "asyncio.Task[None]":
    """The dashboard's Start, shaped like ``_do_background_reconnect`` in
    upstream_admin.py: a tracked background connect that records a failure
    unless it was cancelled or its connect was aborted by a Stop."""

    async def start() -> None:
        try:
            await mgr.connect_upstream(upstream)
        except asyncio.CancelledError:
            raise
        except ConnectAborted:
            return
        except Exception as exc:
            recorded_errors.append(str(exc))

    task = asyncio.create_task(start())
    mgr.register_background_connect_task(upstream.id, task)
    return task


@pytest.mark.asyncio
async def test_a_lazy_attach_that_fails_marks_a_cached_upstream_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached upstream reads as Ready (DEFERRED_ATTACH). When the lazy
    attach that should revive it fails, the dashboard must say so."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    await mgr.transition_to_deferred_attach(
        upstream.id,
        server_info=ServerInfo(name="srv", version="1"),
        self_description=UpstreamSelfDescription(name="srv", version="1"),
    )
    assert mgr.is_connected(upstream.id), "precondition: cached reads Ready"
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        mgr, "_create_task",
        make_failing_create_task(entered, release, "sandbox create failed"),
    )
    release.set()

    with pytest.raises(RuntimeError):
        await mgr.ensure_shared_connected(upstream)

    state = mgr.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    assert state.last_failure == "sandbox create failed"
    assert not mgr.is_connected(upstream.id)
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_lazy_attach_that_joined_a_failing_heal_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing connect was started by a heal, whose body does not mark
    anything FAILED. The tool call that joined it must still leave the
    dashboard showing the failure, not Ready."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    stale = await mgr.connect_shared(upstream)
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        mgr, "_create_task",
        make_failing_create_task(entered, release, "sandbox gone"),
    )

    heal = asyncio.create_task(mgr.reconnect_shared_fresh(upstream, stale=stale))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    lazy = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    heal_outcome, lazy_outcome = await asyncio.gather(
        heal, lazy, return_exceptions=True,
    )

    assert isinstance(heal_outcome, RuntimeError)
    assert isinstance(lazy_outcome, RuntimeError)
    state = mgr.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED, (
        f"the reopen failed but the upstream reads as {state.state.value}"
    )
    assert not mgr.is_connected(upstream.id)
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_start_that_joined_a_failing_lazy_attach_records_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call's lazy attach is connecting when the admin clicks Start,
    which joins it. The connect fails. Start must receive the failure and
    record it, not be cancelled by the lazy attach's own bookkeeping (which
    would make it read as a Stop and drop the error)."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        mgr, "_create_task",
        make_failing_create_task(entered, release, "sandbox create failed"),
    )
    recorded_errors: list[str] = []

    tool_call = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    start = make_start(mgr, upstream, recorded_errors)
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(tool_call, start, return_exceptions=True)

    assert isinstance(outcomes[0], RuntimeError)
    assert not isinstance(outcomes[1], asyncio.CancelledError), (
        "Start was cancelled by the lazy attach's failure handling"
    )
    assert recorded_errors == ["sandbox create failed"]
    await mgr.stop_all()


class _ParkedConnectionStore(FileConnectionStore):
    """Holds the connect's first post-connect write, the moment after the
    session went live but before the connect finished its bookkeeping."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def set_started_config_hash(
        self, org_id: str, upstream_id: str, config_hash: str,
    ) -> None:
        self.parked.set()
        await self.release.wait()
        await super().set_started_config_hash(org_id, upstream_id, config_hash)


@pytest.mark.asyncio
async def test_stop_just_after_start_went_live_reads_as_a_stop(
    tmp_path: Path,
) -> None:
    """Stop lands after Start's connect went live (which stops tracking
    Start as the background task) but before the connect finished. Start
    must see the abort (``ConnectAborted``), which the dashboard route
    treats like a cancel, not a connect error it would record and show.
    (Second review, finding 2.)"""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    store = _ParkedConnectionStore(tmp_path)
    mgr = UpstreamClientManager(
        upstreams=[upstream],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
        connection_store=store,
    )
    recorded_errors: list[str] = []
    start = make_start(mgr, upstream, recorded_errors)
    await asyncio.wait_for(store.parked.wait(), timeout=5.0)

    await asyncio.wait_for(mgr.disconnect_upstream(upstream.id), timeout=5.0)
    store.release.set()
    await asyncio.wait_for(asyncio.gather(start, return_exceptions=True), 5.0)

    assert recorded_errors == [], (
        f"Stop made Start record an error: {recorded_errors}"
    )
    state = mgr.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    assert state.shared_session is None
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_stop_waits_for_the_aborted_connect_before_killing_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop kills the persisted sandbox last. The connect it aborted must
    have finished unwinding by then, or its teardown could run after the
    kill and touch the sandbox record again."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    connect_done_at_kill: list[bool] = []
    real_kill = fake.kill_persisted_session

    async def recording_kill(*, org_id: str, upstream_id: str) -> None:
        flight = mgr._shared_flights._flights.get(upstream_id)  # pyright: ignore[reportPrivateUsage]
        connect_done_at_kill.append(flight is None or flight.task.done())
        await real_kill(org_id=org_id, upstream_id=upstream_id)

    monkeypatch.setattr(fake, "kill_persisted_session", recording_kill)
    waiting = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)

    await asyncio.wait_for(mgr.disconnect_upstream(upstream.id), timeout=5.0)
    gate.set()
    await asyncio.gather(waiting, return_exceptions=True)

    assert connect_done_at_kill == [True], (
        "the aborted connect was still running when Stop killed the sandbox"
    )
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_shutdown_stops_a_connect_in_flight() -> None:
    """``stop_all`` tears the manager down. A connect still running must
    be stopped, or it lands a session in a manager that is gone."""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    waiting = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await wait_until(lambda: fake.session_open_count >= 1)

    await asyncio.wait_for(mgr.stop_all(), timeout=10.0)
    gate.set()
    outcome = await asyncio.gather(waiting, return_exceptions=True)
    await asyncio.sleep(0.2)  # room for an un-stopped connect to land

    assert isinstance(outcome[0], ConnectAborted)
    assert mgr.get_state(upstream.id) is None, (
        "a connect finished after shutdown and recreated the upstream"
    )


@pytest.mark.asyncio
async def test_start_and_boot_reuse_a_live_session() -> None:
    """``connect_shared`` (Start, boot, discovery) reuses a live session
    rather than reopening it. Start disconnects before it connects, so a
    live session at that point was built after the click."""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    first = await mgr.connect_shared(upstream)
    fake.preserve_calls.clear()

    again = await mgr.connect_shared(upstream)
    via_start = await mgr.connect_upstream(upstream)

    assert again is first and via_start is first
    assert fake.session_open_count == 1
    assert fake.preserve_calls == [], "nothing was reopened, so nothing kept"
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_stop_during_a_heal_is_not_logged_as_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin Stop that lands while a tool call's heal is reopening the
    session aborts the heal. That is expected; it must not be logged at
    ERROR, which is what raises a Sentry alert. (Second review, finding 7.)"""
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(
            gate, hold=lambda index: index == 2,
        ),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    registry = ToolRegistry([upstream], mgr)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(
            upstream_id="everything2",
            original_name="echo",
            annotations=ToolAnnotations(idempotentHint=True),
        ),
    ]
    router = ToolRouter(
        registry, mgr, FileAuditRepository(tmp_path / "audit.jsonl"),
        [upstream], policy_engine=PolicyEngine(SettingsConfig()),
    )
    monkeypatch.setattr(
        tool_router_module, "dispatch_with_liveness",
        functools.partial(
            dispatch_with_liveness, probe_interval=0.05, ping_timeout=0.05,
        ),
    )
    await mgr.connect_shared(upstream)
    handle = fake.last_session
    assert handle is not None
    handle.stall()

    with structlog.testing.capture_logs() as logs:
        call = asyncio.create_task(router.route_call(
            org_id="default",
            prefixed_name="everything2__echo",
            arguments={"message": "x"},
            user_id="alice@co.com",
            session_id=None,
        ))
        await asyncio.wait_for(
            wait_until(lambda: fake.session_open_count >= 2), timeout=10.0,
        )
        await asyncio.wait_for(mgr.disconnect_upstream(upstream.id), 10.0)
        result = await asyncio.wait_for(call, timeout=10.0)

    assert result.isError
    heal_failures = [
        entry for entry in logs
        if entry.get("event") == "upstream.dispatch.heal_failed"
    ]
    assert heal_failures, "precondition: the Stop aborted the heal"
    assert all(entry["log_level"] == "warning" for entry in heal_failures), (
        f"a Stop-aborted heal was logged as {heal_failures}"
    )
    gate.set()
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_heal_that_fails_alone_marks_the_upstream_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heal's reopen fails and no tool call is waiting on it. The
    dashboard must not keep showing Ready. (Second review, pass 2.)"""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    stale = await mgr.connect_shared(upstream)
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        mgr, "_create_task",
        make_failing_create_task(entered, release, "sandbox gone"),
    )
    release.set()

    with pytest.raises(RuntimeError):
        await mgr.reconnect_shared_fresh(upstream, stale=stale)

    state = mgr.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    assert state.last_failure == "sandbox gone"
    assert not mgr.is_connected(upstream.id)
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_failure_the_tool_call_walked_away_from_still_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call starts the lazy attach, a heal joins it, then the tool
    call's client hangs up. The heal keeps the connect alive; it fails.
    The only caller left is the heal, and it must still record the
    failure. (Second review, pass 2.)"""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    stale = await mgr.connect_shared(upstream)
    handle = fake.last_session
    assert handle is not None
    handle.kill()  # the wake: the tool call will lazily reattach
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        mgr, "_create_task",
        make_failing_create_task(entered, release, "sandbox gone"),
    )
    tool_call = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    heal = asyncio.create_task(mgr.reconnect_shared_fresh(upstream, stale=stale))
    for _ in range(20):
        await asyncio.sleep(0)
    tool_call.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(tool_call, heal, return_exceptions=True)

    assert isinstance(outcomes[1], RuntimeError), "precondition: it failed"
    state = mgr.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_stop_in_the_same_turn_as_a_failure_stays_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy attach fails on its own in the same event-loop turn a Stop
    arrives: Stop marks DISABLED first, and the connect is already over so
    there is nothing to abort. The tool call's failure handling must not
    then overwrite the admin's Stop with FAILED. (Second review, pass 2.)"""
    fake = make_fake_sandbox_service(server_factory=make_echo_server)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    await mgr.transition_to_deferred_attach(
        upstream.id,
        server_info=ServerInfo(name="srv", version="1"),
        self_description=UpstreamSelfDescription(name="srv", version="1"),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        mgr, "_create_task",
        make_failing_create_task(entered, release, "sandbox create failed"),
    )
    tool_call = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    release.set()
    stop = asyncio.create_task(mgr.disconnect_upstream(upstream.id))
    await asyncio.gather(tool_call, stop, return_exceptions=True)

    state = mgr.get_state(upstream.id)
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED, (
        f"the admin stopped it, but it reads as {state.state.value}"
    )
    await mgr.stop_all()
