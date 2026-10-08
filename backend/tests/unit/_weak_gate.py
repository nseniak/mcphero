"""A wait point that only a weak reference can reach.

The event loop keeps only weak references to tasks. A background job
parked in ``WeakGate.pass_through`` waits on a future that nothing else
references, so the task running the job is reachable only through that
future: unless the code that started the job keeps a strong reference
to the task, ``gc.collect()`` destroys the job mid-flight ("Task was
destroyed but it is pending!").

Tests park the job under test here, collect, then let it through:

    gate = make_weak_gate()
    ...start the background job, which reaches gate.pass_through()...
    await gate.wait_until_parked()
    assert gate.open_after_collect(), "the job was garbage-collected"

The fakes that park in the gate must hold nothing that leads back to
the job's task, or they would keep it alive and hide the bug.
"""
from __future__ import annotations

import asyncio
import gc
import weakref


class WeakGate:
    def __init__(self) -> None:
        self._parked: weakref.ref[asyncio.Future[None]] | None = None
        self._reached = asyncio.Event()

    async def pass_through(self) -> None:
        """Park until ``open_after_collect`` lets the job through."""
        future: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        self._parked = weakref.ref(future)
        self._reached.set()
        await future

    async def wait_until_parked(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._reached.wait(), timeout)

    def open_after_collect(self) -> bool:
        """Run a full garbage collection, then let the parked job through.

        Returns ``False`` when the collection destroyed the job.
        """
        gc.collect()
        future = self._parked() if self._parked is not None else None
        if future is None:
            return False
        future.set_result(None)
        return True


def make_weak_gate() -> WeakGate:
    return WeakGate()
