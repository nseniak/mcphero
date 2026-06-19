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


def reattach_events_since(cursor_ns: int) -> list[dict[str, object]]:
    """Captured ``sandbox.e2b.reattach.ok`` events recorded at/after
    ``cursor_ns`` (a ``time.monotonic_ns()`` value taken by the caller
    before the action it expects to trigger a reattach)."""
    out: list[dict[str, object]] = []
    for ev in _CAPTURED_EVENTS:
        if ev.get("event") != "sandbox.e2b.reattach.ok":
            continue
        ts = ev.get("_recorded_ns")
        if isinstance(ts, int) and ts >= cursor_ns:
            out.append(ev)
    return out


__all__ = ["reattach_events_since"]
