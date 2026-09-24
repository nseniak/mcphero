"""The rules of ``SessionSingleFlight`` in isolation: who waits, who joins,
and who starts a connect of their own.

The manager-level tests (``test_manager_concurrency.py``,
``test_user_session_race.py``) prove the rules through real transports.
These pin the corner cases that are hard to reach from there: a connect
that was cancelled but is still winding down, and a request that arrives
while a deliberate re-sign-in is queued or running. The "sessions" here are
plain objects; the helper never looks inside them.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.upstream_clients.session_single_flight import (
    ConnectAborted,
    SessionSingleFlight,
)

KEY = ("alice@co.com", "drop")


def make_flights() -> SessionSingleFlight[tuple[str, str]]:
    return SessionSingleFlight(
        "user", lambda key: {"user": key[0], "upstream_id": key[1]},
    )


def make_session(name: str) -> ClientSession:
    return cast(ClientSession, name)


def nothing_live() -> ClientSession | None:
    return None


def make_opener(
    session: ClientSession,
    *,
    gate: asyncio.Event | None = None,
    error: Exception | None = None,
    opened: list[str] | None = None,
) -> Callable[[], Awaitable[ClientSession]]:
    """A connect that optionally waits on ``gate``, then fails with
    ``error`` or returns ``session``. Appends to ``opened`` when it runs."""

    async def open_session() -> ClientSession:
        if opened is not None:
            opened.append(str(session))
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        return session

    return open_session


async def let_others_run() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_caller_never_joins_a_connect_that_is_winding_down() -> None:
    """The last waiter left, so the connect was cancelled; it is still
    unwinding when the next caller arrives. That caller must wait for it
    to finish and then start its own. Joining it would hand back a
    cancellation that was never meant for this caller."""
    flights = make_flights()
    parked = asyncio.Event()
    unwind = asyncio.Event()

    async def slow_to_unwind() -> ClientSession:
        try:
            parked.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await unwind.wait()
            raise
        return make_session("never")

    first = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live, open_session=slow_to_unwind,
    ))
    await parked.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # The abandoned connect is still unwinding.
    assert not flights.in_flight(KEY)

    fresh = make_session("fresh")
    second = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live, open_session=make_opener(fresh),
    ))
    await let_others_run()
    assert not second.done(), "the caller must wait out the unwinding connect"
    unwind.set()
    assert await asyncio.wait_for(second, timeout=5) == fresh


@pytest.mark.asyncio
async def test_a_request_during_a_re_sign_in_never_joins_the_older_connect() -> None:
    """A re-sign-in is waiting out an older connect when a request
    arrives. The request must not join that older connect: the re-sign-in
    is about to close its session. It waits for the re-sign-in and then
    decides again (here nothing is live, so it connects on its own)."""
    flights = make_flights()
    old_gate = asyncio.Event()
    old = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live,
        open_session=make_opener(make_session("old"), gate=old_gate),
    ))
    await let_others_run()

    new = make_session("new")
    replace = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(new),
    ))
    await let_others_run()
    late = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live,
        open_session=make_opener(make_session("late-own")),
    ))
    await let_others_run()
    old_gate.set()

    assert await asyncio.wait_for(old, timeout=5) == make_session("old")
    assert await asyncio.wait_for(replace, timeout=5) == new
    assert await asyncio.wait_for(late, timeout=5) == make_session("late-own"), (
        "with no live session left to reuse, the late request connects "
        "on its own rather than adopting the old or the failed attempt"
    )


@pytest.mark.asyncio
async def test_a_request_during_a_failing_re_sign_in_never_gets_its_error() -> None:
    """The re-sign-in's connect fails. That failure belongs to the
    sign-in, whose caller reports it. A request that arrived meanwhile
    must decide again afterwards and run its own connect, with its own
    failure handling."""
    flights = make_flights()
    gate = asyncio.Event()
    replace = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(
            make_session("new"), gate=gate,
            error=RuntimeError("sign-in connect failed at 10.0.0.5"),
        ),
    ))
    await let_others_run()
    own = make_session("own")
    request = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live, open_session=make_opener(own),
    ))
    await let_others_run()
    gate.set()

    with pytest.raises(RuntimeError):
        await replace
    assert await asyncio.wait_for(request, timeout=5) == own


@pytest.mark.asyncio
async def test_a_request_during_a_successful_re_sign_in_reuses_it() -> None:
    flights = make_flights()
    live: list[ClientSession] = []
    gate = asyncio.Event()
    new = make_session("new")

    async def sign_in() -> ClientSession:
        await gate.wait()
        live.append(new)
        return new

    replace = asyncio.create_task(flights.replace(KEY, open_session=sign_in))
    await let_others_run()
    opened: list[str] = []
    request = asyncio.create_task(flights.ensure(
        KEY, current=lambda: live[-1] if live else None,
        open_session=make_opener(make_session("own"), opened=opened),
    ))
    await let_others_run()
    gate.set()

    assert await asyncio.wait_for(replace, timeout=5) == new
    assert await asyncio.wait_for(request, timeout=5) == new
    assert opened == [], "the request must reuse the new session, not connect"


@pytest.mark.asyncio
async def test_queued_re_sign_ins_run_one_after_another() -> None:
    """Two sign-ins in a row: each builds its own session, in order, so
    the latest credentials end up live."""
    flights = make_flights()
    order: list[str] = []
    first_gate = asyncio.Event()

    first = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(
            make_session("first"), gate=first_gate, opened=order,
        ),
    ))
    await let_others_run()
    second = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(make_session("second"), opened=order),
    ))
    await let_others_run()
    assert order == ["first"], "the second sign-in waits for the first"
    first_gate.set()

    assert await asyncio.wait_for(first, timeout=5) == make_session("first")
    assert await asyncio.wait_for(second, timeout=5) == make_session("second")
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_a_connect_that_asks_for_its_own_slot_fails_fast() -> None:
    """A connect that asks for its own slot would wait on itself forever.
    Every entry point must refuse that at once."""
    flights = make_flights()
    outcomes: list[BaseException] = []

    async def re_enter() -> ClientSession:
        for ask in (
            lambda: flights.ensure(
                KEY, current=nothing_live,
                open_session=make_opener(make_session("x")),
            ),
            lambda: flights.renew(
                KEY, current=nothing_live,
                open_session=make_opener(make_session("x")), stale=None,
            ),
            lambda: flights.replace(
                KEY, open_session=make_opener(make_session("x")),
            ),
        ):
            try:
                await ask()
            except RuntimeError as exc:
                outcomes.append(exc)
        return make_session("done")

    result: Any = await asyncio.wait_for(
        flights.ensure(KEY, current=nothing_live, open_session=re_enter),
        timeout=5,
    )
    assert result == make_session("done")
    assert len(outcomes) == 3
    assert all("waited on itself" in str(o) for o in outcomes)


@pytest.mark.asyncio
async def test_nothing_starts_after_a_shutdown() -> None:
    """Shutdown aborts the connect a re-sign-in is waiting out. The
    re-sign-in must not then start a connect of its own: nothing would
    ever stop it, and it would land a session after the teardown.
    (Second review, pass 2.)"""
    flights = make_flights()
    gate = asyncio.Event()
    opened: list[str] = []
    old = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live,
        open_session=make_opener(make_session("old"), gate=gate, opened=opened),
    ))
    await let_others_run()
    replace = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(make_session("new"), opened=opened),
    ))
    await let_others_run()

    aborted = flights.shut_down()
    await asyncio.wait(aborted, timeout=5)
    outcomes = await asyncio.wait_for(
        asyncio.gather(old, replace, return_exceptions=True), timeout=5,
    )

    assert opened == ["old"], f"a connect started after shutdown: {opened}"
    assert all(isinstance(o, ConnectAborted) for o in outcomes), outcomes
    with pytest.raises(ConnectAborted):
        await flights.ensure(
            KEY, current=nothing_live,
            open_session=make_opener(make_session("late")),
        )


@pytest.mark.asyncio
async def test_abort_matching_stops_only_the_matching_connects() -> None:
    """Removing Alice from the org stops every connect of hers, and no
    one else's."""
    flights = make_flights()
    gate = asyncio.Event()
    alice_drop = asyncio.create_task(flights.ensure(
        ("alice@co.com", "drop"), current=nothing_live,
        open_session=make_opener(make_session("a1"), gate=gate),
    ))
    alice_notion = asyncio.create_task(flights.ensure(
        ("alice@co.com", "notion"), current=nothing_live,
        open_session=make_opener(make_session("a2"), gate=gate),
    ))
    bob_drop = asyncio.create_task(flights.ensure(
        ("bob@co.com", "drop"), current=nothing_live,
        open_session=make_opener(make_session("b1"), gate=gate),
    ))
    await let_others_run()

    aborted = flights.abort_matching(lambda key: key[0] == "alice@co.com")
    await asyncio.wait(aborted, timeout=5)
    gate.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(alice_drop, alice_notion, bob_drop, return_exceptions=True),
        timeout=5,
    )

    assert len(aborted) == 2
    assert isinstance(outcomes[0], ConnectAborted)
    assert isinstance(outcomes[1], ConnectAborted)
    assert outcomes[2] == make_session("b1")


@pytest.mark.asyncio
async def test_a_connect_cannot_abort_its_own_slot() -> None:
    """A teardown run from inside the connect it would stop must fail
    loudly: cancelling itself mid-step would fail every waiting caller."""
    flights = make_flights()
    refused: list[RuntimeError] = []

    async def aborts_itself() -> ClientSession:
        try:
            flights.abort(KEY)
        except RuntimeError as exc:
            refused.append(exc)
        return make_session("done")

    result = await asyncio.wait_for(
        flights.ensure(KEY, current=nothing_live, open_session=aborts_itself),
        timeout=5,
    )
    assert result == make_session("done")
    assert len(refused) == 1 and "abort itself" in str(refused[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_first", [False, True])
async def test_a_connect_is_cancelled_once_however_many_ask(stop_first: bool) -> None:
    """The last caller hangs up and an admin clicks Stop, in either order.
    The connect gets ONE cancel: a second one would interrupt it while it
    lets go of its transport."""
    flights = make_flights()
    started = asyncio.Event()
    release_done = asyncio.Event()
    cancels_seen = 0

    async def slow_release() -> ClientSession:
        nonlocal cancels_seen
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancels_seen += 1
        while not release_done.is_set():
            try:
                await release_done.wait()
            except asyncio.CancelledError:
                cancels_seen += 1
        raise asyncio.CancelledError

    caller = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live, open_session=slow_release,
    ))
    await started.wait()
    if stop_first:
        flights.abort(KEY)
        await let_others_run()
        caller.cancel()
    else:
        caller.cancel()
        await let_others_run()
        flights.abort(KEY)
    await let_others_run()
    release_done.set()
    await asyncio.gather(caller, return_exceptions=True)
    await let_others_run()

    assert cancels_seen == 1, f"the connect was cancelled {cancels_seen} times"


@pytest.mark.asyncio
async def test_a_sign_in_queued_behind_an_aborted_connect_never_starts() -> None:
    """A fresh sign-in waits out the connect that is running when the
    user clicks Disconnect. The Disconnect aborts the running connect;
    the waiting sign-in must give up too, not connect right after it with
    the credentials it holds. (Review of the follow-up fixes, F8.)"""
    flights = make_flights()
    gate = asyncio.Event()
    opened: list[str] = []
    running = asyncio.create_task(flights.ensure(
        KEY, current=nothing_live,
        open_session=make_opener(make_session("old"), gate=gate, opened=opened),
    ))
    await let_others_run()
    sign_in = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(make_session("new"), opened=opened),
    ))
    await let_others_run()

    aborted = flights.abort(KEY)  # the Disconnect
    assert aborted is not None
    await asyncio.wait({aborted}, timeout=5)
    outcomes = await asyncio.wait_for(
        asyncio.gather(running, sign_in, return_exceptions=True), timeout=5,
    )

    assert all(isinstance(o, ConnectAborted) for o in outcomes), outcomes
    assert opened == ["old"], f"a connect started after the Disconnect: {opened}"


@pytest.mark.asyncio
async def test_a_sign_in_after_a_disconnect_still_runs() -> None:
    """Only replacements that were waiting at the time give up: a sign-in
    that starts after the Disconnect connects normally."""
    flights = make_flights()
    flights.abort(KEY)  # a Disconnect with nothing running

    session = await asyncio.wait_for(
        flights.replace(KEY, open_session=make_opener(make_session("new"))),
        timeout=5,
    )
    assert session == make_session("new")


@pytest.mark.asyncio
async def test_removing_a_user_between_two_queued_sign_ins_stops_the_next() -> None:
    """For a moment between one queued sign-in finishing and the next one
    starting, nothing runs for the slot. A user removal landing then must
    still stop the next sign-in. (Review pass 2, N2.)"""
    flights = make_flights()
    # An earlier sign-in still queued, and nothing running.
    earlier: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    flights._replacements[KEY] = [earlier]  # pyright: ignore[reportPrivateUsage]
    opened: list[str] = []
    sign_in = asyncio.create_task(flights.replace(
        KEY, open_session=make_opener(make_session("new"), opened=opened),
    ))
    await let_others_run()

    flights.abort_matching(lambda key: key[0] == "alice@co.com")
    earlier.set_result(None)
    outcome = await asyncio.wait_for(
        asyncio.gather(sign_in, return_exceptions=True), timeout=5,
    )

    assert isinstance(outcome[0], ConnectAborted), outcome
    assert opened == [], "a sign-in started after the user was removed"
