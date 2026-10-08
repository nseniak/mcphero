"""``Backoff``: after each attempt in a row, the next one waits twice as
long, up to a ceiling; a success starts over. The schedule a reconnect's
forced token refresh follows per sign-in (1, 2, 4, 8, 16, then every 30
minutes) is checked here on a fake clock."""
from __future__ import annotations

from mcpolis.adapters.upstream_clients.client_manager import (
    FORCED_REFRESH_FIRST_DELAY_SECONDS,
    FORCED_REFRESH_MAX_DELAY_SECONDS,
)
from mcpolis.domain.services.backoff import Backoff

MINUTE = 60.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_forced_refresh_backoff(clock: FakeClock) -> Backoff[str]:
    return Backoff(
        first_delay_seconds=FORCED_REFRESH_FIRST_DELAY_SECONDS,
        max_delay_seconds=FORCED_REFRESH_MAX_DELAY_SECONDS,
        now=clock,
    )


def minutes_between_attempts(backoff: Backoff[str], clock: FakeClock, count: int) -> list[float]:
    """Try every second for ``count`` attempts; the gaps between them."""
    times: list[float] = []
    while len(times) < count:
        if backoff.attempt("alice"):
            times.append(clock.now)
        clock.now += 1
    return [(b - a) / MINUTE for a, b in zip(times, times[1:])]


def test_the_forced_refresh_schedule_doubles_up_to_half_an_hour() -> None:
    clock = FakeClock()
    backoff = make_forced_refresh_backoff(clock)

    gaps = minutes_between_attempts(backoff, clock, 8)

    assert gaps == [1, 2, 4, 8, 16, 30, 30]


def test_an_attempt_waits_its_turn() -> None:
    clock = FakeClock()
    backoff = make_forced_refresh_backoff(clock)

    assert backoff.attempt("alice")
    clock.now += MINUTE - 1
    assert not backoff.attempt("alice")
    clock.now += 1
    assert backoff.attempt("alice")


def test_a_reset_lets_the_next_attempt_go_at_once() -> None:
    clock = FakeClock()
    backoff = make_forced_refresh_backoff(clock)
    assert backoff.attempt("alice")

    backoff.reset("alice")

    assert backoff.attempt("alice")


def test_keys_back_off_on_their_own() -> None:
    clock = FakeClock()
    backoff = make_forced_refresh_backoff(clock)
    assert backoff.attempt("alice")

    assert backoff.attempt("bob")
    assert not backoff.attempt("alice")


def test_a_key_quiet_for_two_ceilings_starts_its_streak_over() -> None:
    clock = FakeClock()
    backoff = make_forced_refresh_backoff(clock)
    for _ in range(6):  # a long streak: the next wait is the ceiling
        clock.now += FORCED_REFRESH_MAX_DELAY_SECONDS
        assert backoff.attempt("alice")

    clock.now += 2 * FORCED_REFRESH_MAX_DELAY_SECONDS
    assert backoff.attempt("alice")

    clock.now += MINUTE
    assert backoff.attempt("alice"), "the streak did not start over"
