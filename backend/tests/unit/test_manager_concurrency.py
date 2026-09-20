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
replaces ``connect_shared`` with a stub) and the ``_FakeTask`` cross-pool
race in ``test_ensure_shared_connected_heals_dead_session.py``: here the
session is a real ``ClientSession`` and the coalesced callers go on to make
a real ``call_tool`` on the healed session — the end-to-end "single-flight
coalesces AND the survivors land on a usable transport" contract.

NOTE: no ``from __future__ import annotations`` — FastMCP tool registration
calls ``issubclass()`` on annotations, which breaks under stringified
annotations (see the same note in ``fake_sandbox_service.py``).
"""
import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager

import pytest
from mcp.server.fastmcp import FastMCP

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from tests.unit.factories import make_upstream_definition
from tests.unit.fake_sandbox_service import (
    FakeSandboxService,
    make_fake_sandbox_service,
)


def make_manager(
    upstream: UpstreamDefinition, fake: FakeSandboxService,
) -> UpstreamClientManager:
    """Wire a real manager to the in-memory fake under the ``e2b`` provider
    (the fake reports ``name = "e2b"`` so it stands in for the production
    backend in provider-keyed lookups)."""
    return UpstreamClientManager(
        upstreams=[upstream],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
    )


def make_echo_server() -> FastMCP:
    server = FastMCP(name="ConcUpstream")

    @server.tool(name="echo", description="Echo back the message")
    def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"echo:{message}"

    return server


def make_gated_server_factory(
    gate: asyncio.Event,
) -> Callable[[], FastMCP]:
    """A ``server_factory`` whose FIRST session blocks inside its lifespan
    until ``gate`` is set, holding that session's ``initialize`` (hence the
    in-flight ``connect_shared``) parked. Every later session starts
    immediately, so a coalesced sibling that opened its own session would
    still complete — making a single-flight regression visible as a
    ``session_open_count`` > 1 rather than a deadlock.
    """
    calls = 0

    def factory() -> FastMCP:
        nonlocal calls
        calls += 1
        index = calls

        @asynccontextmanager
        async def lifespan(_server: FastMCP):  # type: ignore[no-untyped-def]
            if index == 1:
                await gate.wait()
            yield {}

        server = FastMCP(name="GatedUpstream", lifespan=lifespan)

        @server.tool(name="echo", description="Echo back the message")
        def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
            return f"echo:{message}"

        return server

    return factory


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Yield to the event loop until ``predicate`` holds. Deterministic —
    spins on ``asyncio.sleep(0)`` (no wall-clock sleep), so it advances the
    loop without introducing a timing dependency."""
    while not predicate():
        await asyncio.sleep(0)


# --- CONC-2: concurrent reconnect_shared_fresh coalesces to ONE session,
#     and every caller lands on the healed, usable session -----------------


@pytest.mark.asyncio
async def test_concurrent_reconnect_coalesces_to_one_real_session() -> None:
    """CONC-2: N concurrent ``reconnect_shared_fresh`` while the first is
    held in-flight open exactly ONE new sandbox (single-flight coalesces),
    and after the heal every caller can ``call_tool`` on the fresh session.

    The first reconnect's ``connect_shared`` is parked inside the gated
    lifespan; the other N-1 pile onto ``_reconnect_fresh_tasks`` and await
    it. A missing single-flight would show as ``session_open_count`` > 1
    (N sandboxes, N-1 orphaned)."""
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

    await mgr.stop_all()


# --- CONC-3: a lazy-connect failure under contention clears the slot and
#     fails BOTH callers, leaving the upstream FAILED ----------------------


@pytest.mark.asyncio
async def test_lazy_connect_failure_under_contention_clears_slot() -> None:
    """CONC-3: when the in-flight ``connect_shared`` fails, two concurrent
    ``ensure_shared_connected`` callers must BOTH observe the failure, the
    ``_lazy_connect_tasks`` single-flight slot must be emptied (so the next
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

    assert mgr._lazy_connect_tasks == {}, (  # pyright: ignore[reportPrivateUsage]
        "the single-flight slot must be cleared on failure so the next "
        "dispatch retries instead of awaiting a dead task"
    )
    state = mgr.get_state("everything2")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED, (
        "a failed lazy attach must mark the upstream FAILED"
    )

    await mgr.stop_all()
