"""One refresh of a sign-in's tokens at a time.

Many upstreams accept each refresh token once: a refresh hands out a new
one and retires the old. Two refreshes of one sign-in at the same moment
then race. The second presents the refresh token the first just used up,
the upstream answers ``invalid_grant``, and a refused refresh deletes the
sign-in and emails its owner, although the first refresh's new tokens,
still being saved, work.

So every refresh the gateway starts itself takes this lock, per sign-in
(org, upstream, user): the periodic refresh (``oauth_refresh``), and a
reconnect's refresh and its forced refresh (``upstream_connection_service``).
A refresh the sign-in library makes on its own inside a live session does
not: it runs inside the library's request handling.

- In this process: an ``asyncio.Lock`` per sign-in.
- Across backends (cloud mode): the distributed lock under the same key,
  ``lock:token_refresh:<org>:<upstream>:<user>``, the key the periodic
  refresh always used. A Mongo lock is held per backend, not per task, so
  the in-process lock is taken first.

The periodic refresh takes it only when it is free (``hold_if_free``):
another refresh of the sign-in is under way, so this round skips it. A
reconnect waits for it (``hold``), ``wait_seconds`` at most: the tokens it
reads afterwards are the ones the other refresh saved.

The distributed lock lives ``ttl_seconds`` and is renewed every third of
that while held, so a refresh keeps it however long it runs, and a lock
a backend left when it died frees itself soon. Each try at it is bounded
too: a store that never answers holds the caller no longer than its wait.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

import structlog

from mcpolis.domain.ports.distributed_lock import DistributedLock

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# How long a reconnect waits for another refresh of the same sign-in. A
# refresh is one round trip to the upstream's token endpoint and one store
# write, about a second. The periodic refresh's worst case (three probes
# of up to 10 s, 5 s apart) is far longer, and a tool call waiting on the
# reconnect should not wait that out.
REFRESH_LOCK_WAIT_SECONDS = 10.0
# Lifetime of the distributed lock, renewed every third of it while held:
# a refresh keeps the lock as long as it runs (the periodic refresh's
# worst case is about 40 s), through a failed renewal. A
# backend that dies holding it (killed, out of memory) leaves a lock that
# frees itself within this lifetime. It is no longer than a reconnect's
# wait, so a reconnect that finds such a lock waits it out and refreshes,
# instead of failing its tool call; the periodic refresh skips the sign-in
# for one round at most. (It was 60 s, with no renewal: tool calls of the
# sign-in failed for up to a minute after such a death.)
REFRESH_LOCK_TTL_SECONDS = REFRESH_LOCK_WAIT_SECONDS
# How long one try at the distributed lock may take at least: one round
# trip to its store, which answers in milliseconds. A try is bounded by
# what is left of the caller's wait, or by this when less is left (the
# periodic refresh does not wait at all). Unbounded, a store that never
# answers held the caller for the store's own timeout (Mongo: 30 s).
DISTRIBUTED_TRY_SECONDS = 2.0
# How often a waiting reconnect tries the distributed lock again.
_DISTRIBUTED_POLL_SECONDS = 0.25


@dataclass
class _LocalLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Callers holding or waiting for ``lock``: the entry is dropped when
    # the last one leaves, so sign-ins nobody refreshes take no memory.
    users: int = 0


class SignInRefreshLock:
    def __init__(
        self,
        distributed_lock: DistributedLock | None = None,
        *,
        wait_seconds: float = REFRESH_LOCK_WAIT_SECONDS,
        ttl_seconds: float = REFRESH_LOCK_TTL_SECONDS,
        try_seconds: float = DISTRIBUTED_TRY_SECONDS,
    ) -> None:
        self._distributed = distributed_lock
        self._wait_seconds = wait_seconds
        self._ttl_seconds = ttl_seconds
        self._try_seconds = try_seconds
        self._local: dict[tuple[str, str, str], _LocalLock] = {}

    @staticmethod
    def key(org_id: str, upstream_id: str, user_id: str) -> str:
        """The distributed lock's key for one sign-in."""
        return f"lock:token_refresh:{org_id}:{upstream_id}:{user_id}"

    def is_held(self, org_id: str, upstream_id: str, user_id: str) -> bool:
        """Whether a refresh of the sign-in holds the lock in this
        process."""
        entry = self._local.get((org_id, upstream_id, user_id))
        return entry is not None and entry.lock.locked()

    def waiting(self, org_id: str, upstream_id: str, user_id: str) -> int:
        """How many callers in this process wait for the sign-in's lock
        (a reconnect waiting for the periodic refresh to save)."""
        entry = self._local.get((org_id, upstream_id, user_id))
        if entry is None:
            return 0
        return entry.users - (1 if entry.lock.locked() else 0)

    def hold_if_free(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> AbstractAsyncContextManager[bool]:
        """Hold the lock when nobody does; yields whether it holds it."""
        return self._hold(org_id, upstream_id, user_id, wait_seconds=0.0)

    def hold(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> AbstractAsyncContextManager[bool]:
        """Hold the lock, waiting ``wait_seconds`` at most for the refresh
        holding it; yields whether it holds it."""
        return self._hold(
            org_id, upstream_id, user_id, wait_seconds=self._wait_seconds,
        )

    @asynccontextmanager
    async def _hold(
        self,
        org_id: str,
        upstream_id: str,
        user_id: str,
        *,
        wait_seconds: float,
    ) -> AsyncIterator[bool]:
        local_key = (org_id, upstream_id, user_id)
        entry = self._local.get(local_key)
        if entry is None:
            entry = self._local[local_key] = _LocalLock()
        entry.users += 1
        name = self.key(org_id, upstream_id, user_id)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        held_here = held_everywhere = False
        released = asyncio.Event()
        renewal: asyncio.Task[None] | None = None
        try:
            held_here = await _take(entry.lock, wait_seconds)
            if held_here:
                held_everywhere = await self._take_distributed(name, deadline)
            if held_everywhere and self._distributed is not None:
                renewal = asyncio.create_task(
                    self._renew_until(released, self._distributed, name),
                    name=f"refresh-lock-renewal:{name}",
                )
            yield held_everywhere
        finally:
            try:
                if renewal is not None:
                    # Ended before the release (a renewal under way is
                    # waited for, a third of the lifetime at most), so no
                    # renewal lands after it and takes the lock back.
                    released.set()
                    await asyncio.wait({renewal})
                if held_everywhere:
                    await self._release_distributed(name)
            finally:
                if held_here:
                    entry.lock.release()
                entry.users -= 1
                if entry.users == 0:
                    del self._local[local_key]

    async def _take_distributed(self, name: str, deadline: float) -> bool:
        if self._distributed is None:
            return True
        loop = asyncio.get_running_loop()
        while True:
            # One try, bounded by what is left of the caller's wait, and
            # never by less than one round trip to the store.
            try_seconds = max(deadline - loop.time(), self._try_seconds)
            try:
                async with asyncio.timeout(try_seconds):
                    acquired = await self._distributed.acquire(
                        name, ttl_seconds=self._ttl_seconds,
                    )
            except TimeoutError:
                logger.warning(
                    "oauth.token.refresh_lock.timed_out",
                    key=name, timeout_seconds=round(try_seconds, 3),
                )
                return False
            except Exception:
                logger.warning(
                    "oauth.token.refresh_lock.failed", key=name, exc_info=True,
                )
                return False
            if acquired:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_DISTRIBUTED_POLL_SECONDS, remaining))

    async def _renew_until(
        self, released: asyncio.Event, distributed: DistributedLock, name: str,
    ) -> None:
        """Renew the distributed lock's lifetime every third of it, until
        ``released`` is set (the hold ended). Each try is bounded by that
        third; a failed one is logged, and the next one may still make it.
        Ends when another backend holds the lock: this one's lifetime ran
        out while its renewals failed."""
        every = self._ttl_seconds / 3
        while True:
            try:
                await asyncio.wait_for(released.wait(), timeout=every)
            except TimeoutError:
                pass
            else:
                return
            try:
                async with asyncio.timeout(every):
                    # Taking a lock this backend holds renews its lifetime.
                    renewed = await distributed.acquire(
                        name, ttl_seconds=self._ttl_seconds,
                    )
            except Exception:
                logger.warning(
                    "oauth.token.refresh_lock.renew_failed",
                    key=name, exc_info=True,
                )
                continue
            if not renewed:
                logger.warning("oauth.token.refresh_lock.lost", key=name)
                return

    async def _release_distributed(self, name: str) -> None:
        if self._distributed is None:
            return
        try:
            await self._distributed.release(name)
        except Exception:
            # Its lifetime frees it.
            logger.warning(
                "oauth.token.refresh_lock.release_failed",
                key=name, exc_info=True,
            )


async def _take(lock: asyncio.Lock, wait_seconds: float) -> bool:
    """Acquire ``lock`` within ``wait_seconds``; with 0, only when it is
    free right now (``acquire`` returns without waiting then)."""
    try:
        async with asyncio.timeout(max(wait_seconds, 0.0)):
            await lock.acquire()
    except TimeoutError:
        return False
    return True
