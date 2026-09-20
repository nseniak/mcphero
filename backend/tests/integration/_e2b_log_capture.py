"""Shared structlog capture for the real-E2B integration tests.

Several integration files need to assert on ``sandbox.e2b.reattach.ok``
events without parsing stderr. ``structlog.configure`` is PROCESS-GLOBAL,
so a per-file capture processor clobbers every other file's: only the
last-imported ``configure`` stays active, leaving the losers' capture lists
permanently empty. Under ``--dist loadfile`` (where one worker imports every
collected module) that left E2B-M4's capture empty whenever the targeted
file's ``configure`` won the race — a green-looking product run failing on a
phantom "reattach.ok did not fire".

The fix is ONE shared capture, imported by every file that needs it:
``structlog`` is configured exactly once (this module runs at first import
and is cached), appending to ONE list. Tests isolate their own events with
``reattach_events_since(cursor_ns)`` (a monotonic cursor), and separate xdist
workers are separate processes — each with its own copy of this module — so
the shared list is safe under parallelism.
"""
from __future__ import annotations

import time

import structlog
from structlog.typing import EventDict, WrappedLogger

_CAPTURED_EVENTS: list[dict[str, object]] = []


def _capture_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict,
) -> EventDict:
    del logger, method_name
    event_dict["_recorded_ns"] = time.monotonic_ns()
    _CAPTURED_EVENTS.append(dict(event_dict))
    return event_dict


structlog.configure(
    processors=[
        _capture_processor,
        structlog.processors.KeyValueRenderer(
            key_order=["event"], drop_missing=True,
        ),
    ],
)


def events_since(name: str, cursor_ns: int) -> list[dict[str, object]]:
    """Captured events called *name* recorded at/after ``cursor_ns``.

    ``cursor_ns`` is a ``time.monotonic_ns()`` value the caller takes
    before the action it expects to produce the event.
    """
    out: list[dict[str, object]] = []
    for ev in _CAPTURED_EVENTS:
        if ev.get("event") != name:
            continue
        ts = ev.get("_recorded_ns")
        if isinstance(ts, int) and ts >= cursor_ns:
            out.append(ev)
    return out


def stream_death_events_since(cursor_ns: int) -> list[dict[str, object]]:
    """Captured ``sandbox.e2b.stream_dead`` events since the cursor.

    E2B severs a sandbox's output stream when it pauses it, so the
    watcher task fires at the pause and this is the earliest evidence
    that a pause happened. Two earlier names for the same question
    are gone: ``sandbox.e2b.reattach.ok`` (the service no longer
    reattaches) and ``sandbox.e2b.wake.process_retired`` (the pump
    branch that logged it is now a backstop the watcher usually
    beats).

    CAVEAT for callers: this also fires on ordinary session teardown,
    when the process is killed. It only means "the sandbox paused"
    when the cursor brackets an IDLE window in which nothing else
    touched the session.
    """
    return events_since("sandbox.e2b.stream_dead", cursor_ns)


__all__ = ["events_since", "stream_death_events_since"]
