"""EmitThrottle: one emission per key per interval, nothing lost."""
from __future__ import annotations

from mcpolis.domain.services.emit_throttle import EmitThrottle
from tests.unit.test_rate_limiter import FakeClock


def make_throttle(clock: FakeClock) -> EmitThrottle:
    return EmitThrottle(60.0, now=clock)


def test_first_occurrence_emits_and_the_rest_are_counted() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock)

    first = throttle.attempt("a")
    quiet = [throttle.attempt("a") for _ in range(4)]
    clock.advance(60.0)
    next_emission = throttle.attempt("a")

    assert first == 0
    assert quiet == [None] * 4
    assert next_emission == 4


def test_keys_are_independent() -> None:
    throttle = make_throttle(FakeClock())
    throttle.attempt("a")

    assert throttle.attempt("b") == 0


def test_forget_returns_the_swallowed_count() -> None:
    throttle = make_throttle(FakeClock())
    throttle.attempt("a")
    throttle.attempt("a")
    throttle.attempt("a")

    assert throttle.forget("a") == 2
    assert throttle.forget("a") == 0
    assert throttle.attempt("a") == 0


def test_drain_quiet_hands_back_every_key_gone_quiet_with_its_count() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock)
    throttle.attempt("quiet-with-count")
    throttle.attempt("quiet-with-count")
    throttle.attempt("quiet-no-count")
    clock.advance(119.0)
    throttle.attempt("still-busy")

    assert throttle.drain_quiet() == []  # not two intervals yet
    clock.advance(60.0)  # next scan allowed; "still-busy" isn't quiet yet
    # Keys with nothing to flush come back too, so a caller holding
    # something per key can drop it.
    assert throttle.drain_quiet() == [("quiet-with-count", 1), ("quiet-no-count", 0)]
    # Forgotten: the next occurrence emits again, with nothing carried.
    assert throttle.attempt("quiet-with-count") == 0


def test_drain_quiet_scans_at_most_once_per_interval() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock)
    throttle.attempt("a")
    throttle.attempt("a")
    clock.advance(100.0)
    assert throttle.drain_quiet() == []  # scanned; "a" not quiet yet

    clock.advance(21.0)  # "a" is quiet now, but the last scan was 21 s ago
    assert throttle.drain_quiet() == []

    clock.advance(39.0)  # 60 s since the last scan
    assert throttle.drain_quiet() == [("a", 1)]
