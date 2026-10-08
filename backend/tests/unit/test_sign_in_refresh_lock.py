"""One refresh of a sign-in's tokens at a time (``SignInRefreshLock``).

Upstreams that rotate refresh tokens accept each one once. The periodic
refresh used to race a reconnect's refresh of the same sign-in: the
periodic refresh used refresh token RT1, the upstream issued RT2, and
while RT2 was being saved, a tool call met a 401 and its reconnect
forced a refresh with RT1, still the stored one. The upstream refused
it (``invalid_grant``), the reconnect deleted the sign-in and emailed
the member, and the save of RT2 then found no sign-in to update.

The periodic refresh and a reconnect's refreshes now take one lock per
sign-in: the periodic refresh skips a sign-in being refreshed, and a
reconnect waits (a bounded time) for the periodic refresh to save.

NOTE: no ``from __future__ import annotations`` (FastMCP, see harness).
"""
import asyncio
import time
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import structlog
from starlette.types import Message, Receive, Scope, Send

from mcpolis.adapters.distributed_lock_mongo import MongoDistributedLock
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.mongo_client import MotorDatabase
from mcpolis.domain.ports import DEFAULT_ORG_ID
# ``upstream_connection_service`` first: it and ``oauth_refresh`` import
# each other, and only that order resolves.
from mcpolis.domain.services.upstream_connection_service import (
    TOKEN_REFRESH_MARGIN,
)
from mcpolis.domain.services.oauth_refresh import refresh_token_for_user
from mcpolis.domain.services.sign_in_refresh_lock import (
    REFRESH_LOCK_TTL_SECONDS,
    REFRESH_LOCK_WAIT_SECONDS,
    SignInRefreshLock,
)
from tests.unit._user_session_harness import (
    ALICE,
    BOB,
    GATEWAY_URL,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_upstream,
    make_upstream_server,
    stop_upstream,
    wait_until,
)
from tests.unit.factories import seed_sign_in_age
from tests.unit.mongo_fixture import temp_mongo_database
from tests.unit.test_upstream_401_burst_policy import (
    FlakyAuthUpstream,
    RecordingWarner,
    make_manager,
    make_signed_in_store,
    serve,
)


def is_held(lock: SignInRefreshLock, user: str = ALICE) -> bool:
    return lock.is_held(DEFAULT_ORG_ID, UPSTREAM_ID, user)


def waiting(lock: SignInRefreshLock) -> int:
    return lock.waiting(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE)


# --- The lock itself ---


async def test_a_free_lock_is_held_at_once_and_let_go_after() -> None:
    lock = SignInRefreshLock()

    async with lock.hold_if_free(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
        assert held
        assert is_held(lock)

    assert not is_held(lock)


async def test_hold_if_free_skips_a_sign_in_another_refresh_holds() -> None:
    lock = SignInRefreshLock()

    async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE):
        async with lock.hold_if_free(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
            assert not held

    async with lock.hold_if_free(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
        assert held


async def test_hold_waits_for_the_refresh_that_holds_the_lock() -> None:
    lock = SignInRefreshLock()
    steps: list[str] = []
    release = asyncio.Event()

    async def other_refresh() -> None:
        async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE):
            steps.append("other refresh holds")
            await release.wait()
            steps.append("other refresh saved")

    async def reconnect() -> None:
        async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
            steps.append(f"reconnect holds: {held}")

    other = asyncio.create_task(other_refresh())
    await wait_until(lambda: is_held(lock))
    waiter = asyncio.create_task(reconnect())
    await wait_until(lambda: waiting(lock) == 1)
    release.set()
    await asyncio.gather(other, waiter)

    assert steps == [
        "other refresh holds", "other refresh saved", "reconnect holds: True",
    ]
    assert not is_held(lock)


async def test_hold_gives_up_after_its_wait() -> None:
    lock = SignInRefreshLock(wait_seconds=0.05)

    async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE):
        async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
            assert not held
        assert is_held(lock)


async def test_another_sign_in_does_not_wait() -> None:
    lock = SignInRefreshLock(wait_seconds=0.05)

    async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE):
        async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, BOB) as held:
            assert held


class OtherBackendLock:
    """A distributed lock another backend may hold keys of."""

    def __init__(self) -> None:
        self.held_elsewhere: set[str] = set()
        self.released: list[str] = []
        self.broken = False

    async def acquire(self, key: str, ttl_seconds: float = 30) -> bool:
        del ttl_seconds
        if self.broken:
            raise ConnectionError("lock store unreachable")
        return key not in self.held_elsewhere

    async def release(self, key: str) -> None:
        self.released.append(key)

    async def close(self) -> None:
        pass


async def test_a_refresh_on_another_backend_is_skipped_or_waited_for() -> None:
    other_backend = OtherBackendLock()
    key = SignInRefreshLock.key(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE)
    other_backend.held_elsewhere.add(key)
    lock = SignInRefreshLock(other_backend)

    async with lock.hold_if_free(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
        assert not held
    assert not is_held(lock), "the lock of this process was kept"

    async def other_backend_saves() -> None:
        await asyncio.sleep(0.3)
        other_backend.held_elsewhere.discard(key)

    saving = asyncio.create_task(other_backend_saves())
    async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
        assert held
    await saving

    assert other_backend.released == [key]


async def test_an_unreachable_distributed_lock_is_not_held() -> None:
    other_backend = OtherBackendLock()
    other_backend.broken = True
    lock = SignInRefreshLock(other_backend)

    async with lock.hold_if_free(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
        assert not held

    assert other_backend.released == []
    assert not is_held(lock)


class HungLockStore:
    """A distributed lock whose store never answers: a hung Mongo, which
    only its own socket timeout (30 s) would end."""

    def __init__(self) -> None:
        self.tries = 0

    async def acquire(self, key: str, ttl_seconds: float = 30) -> bool:
        del key, ttl_seconds
        self.tries += 1
        await asyncio.Event().wait()
        return True

    async def release(self, key: str) -> None:
        del key

    async def close(self) -> None:
        pass


async def held_after(
    hold: AbstractAsyncContextManager[bool],
) -> tuple[bool, float]:
    """Whether ``hold`` held the lock, and how long it took to say (5 s at
    most: a hold that would wait for a hung store fails the test)."""
    started = time.monotonic()
    async with asyncio.timeout(5):
        async with hold as held:
            return held, time.monotonic() - started


async def test_a_hung_lock_store_holds_a_reconnect_no_longer_than_its_wait() -> None:
    store = HungLockStore()
    lock = SignInRefreshLock(store, wait_seconds=0.3, try_seconds=0.05)

    held, waited = await held_after(lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE))

    assert not held
    assert 0.25 <= waited < 2.0, waited
    assert store.tries == 1
    assert not is_held(lock)


async def test_a_hung_lock_store_holds_the_periodic_refresh_one_try_at_most() -> None:
    lock = SignInRefreshLock(HungLockStore(), try_seconds=0.1)

    held, waited = await held_after(
        lock.hold_if_free(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE),
    )

    assert not held
    assert waited < 2.0, waited
    assert not is_held(lock)


class RecordingLockStore:
    """A distributed lock that grants every key to this backend and
    records what it was asked, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire(self, key: str, ttl_seconds: float = 30) -> bool:
        del key, ttl_seconds
        self.calls.append("acquire")
        return True

    async def release(self, key: str) -> None:
        del key
        self.calls.append("release")

    async def close(self) -> None:
        pass


async def test_a_held_lock_is_renewed_and_never_after_it_is_let_go() -> None:
    store = RecordingLockStore()
    lock = SignInRefreshLock(store, ttl_seconds=0.3)

    async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
        assert held
        await asyncio.sleep(0.35)  # over three renewal periods (0.1 s)
    await asyncio.sleep(0.3)

    assert store.calls[0] == "acquire"
    assert store.calls.count("acquire") >= 2, store.calls
    assert store.calls[-1] == "release", store.calls
    assert store.calls.count("release") == 1


def test_a_lock_left_by_a_backend_that_died_frees_within_a_reconnects_wait() -> None:
    """A backend that dies holding the lock leaves it for one lifetime: no
    longer than a reconnect waits, so the reconnect waits it out and
    refreshes, instead of failing its tool call (the lifetime was 60 s)."""
    assert REFRESH_LOCK_TTL_SECONDS <= REFRESH_LOCK_WAIT_SECONDS


def make_backend_lock(db: MotorDatabase, *, ttl_seconds: float) -> SignInRefreshLock:
    """The refresh lock of one backend of a cloud deployment: its own
    holder in the shared Mongo."""
    return SignInRefreshLock(MongoDistributedLock(db), ttl_seconds=ttl_seconds)


async def test_a_reconnect_waits_out_a_lock_a_backend_left_when_it_died() -> None:
    """The bound above at work, scaled down: the lock a dead backend left
    lives no longer than a reconnect waits, so the reconnect gets it."""
    async with temp_mongo_database() as db:
        dead_backend = MongoDistributedLock(db)
        key = SignInRefreshLock.key(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE)
        # What the backend left: the lock, as its last renewal set it.
        assert await dead_backend.acquire(key, ttl_seconds=0.5)
        lock = SignInRefreshLock(
            MongoDistributedLock(db), wait_seconds=1.0, ttl_seconds=0.5,
        )

        held, waited = await held_after(lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE))

        assert held
        assert waited >= 0.3, waited


async def test_a_refresh_outlasting_the_locks_lifetime_keeps_the_lock() -> None:
    """The lock is renewed while held: another backend cannot take it from
    a refresh that runs longer than its lifetime (the periodic refresh's
    retries take up to 40 s), and takes it once the refresh lets go."""
    async with temp_mongo_database() as db:
        this_backend = make_backend_lock(db, ttl_seconds=0.6)
        other_backend = make_backend_lock(db, ttl_seconds=0.6)

        async with this_backend.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE) as held:
            assert held
            for _ in range(8):  # 2 s: over three lifetimes
                await asyncio.sleep(0.25)
                async with other_backend.hold_if_free(
                    DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
                ) as taken:
                    assert not taken

        async with other_backend.hold_if_free(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        ) as taken:
            assert taken


# --- The periodic refresh and a reconnect of the same sign-in ---


class RotatingRefreshUpstream(FlakyAuthUpstream):
    """Its token endpoint rotates refresh tokens: each works once. A used
    one presented again is refused, and counted in ``reused`` (an upstream
    with reuse detection revokes the whole sign-in then)."""

    def __init__(self, *, refusals: int) -> None:
        super().__init__(
            make_upstream_server(ConnectionGate()).streamable_http_app(),
            refusals=refusals, token_answer=None,
        )
        self.used: set[str] = set()
        self.issued = 0
        self.reused = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http" and scope["method"] == "POST"
            and scope["path"].rstrip("/") == "/token"
        ):
            body = b""
            while True:
                message: Message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            refresh_token = parse_qs(body.decode()).get("refresh_token", [""])[0]
            self.refresh_requests += 1
            if refresh_token in self.used:
                self.reused += 1
                await self._answer(send, 400, {"error": "invalid_grant"})
                return
            self.used.add(refresh_token)
            self.issued += 1
            await self._answer(send, 200, {
                "access_token": f"at-{self.issued}", "token_type": "Bearer",
                "expires_in": 3600, "refresh_token": f"rt-{self.issued}",
            })
            return
        await super().__call__(scope, receive, send)


class RefreshSaveHeld(FileConnectionStore):
    """Once ``armed``, the next refresh save waits for ``release``: the
    upstream already issued the new tokens and retired the old refresh
    token, and the store does not have them yet."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.armed = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def put_user_token_if_same_sign_in(
        self, *args: Any, **kwargs: Any,
    ) -> str | None:
        if self.armed:
            self.armed = False
            self.reached.set()
            await self.release.wait()
        return await super().put_user_token_if_same_sign_in(*args, **kwargs)


async def start_periodic_refresh_held_at_its_save(
    store: RefreshSaveHeld,
    url: str,
    lock: SignInRefreshLock,
    warner: RecordingWarner,
) -> "asyncio.Task[None]":
    """The periodic refresh of ALICE's sign-in, holding the lock, stopped
    after the upstream answered and before the store has the new tokens."""
    store.armed = True
    periodic = asyncio.create_task(refresh_token_for_user(
        DEFAULT_ORG_ID, make_upstream(url), ALICE, store, GATEWAY_URL,
        refresh_lock=lock, warner=warner,  # type: ignore[arg-type]
    ))
    await asyncio.wait_for(store.reached.wait(), 10)
    return periodic


async def make_store_with_an_aged_sign_in(tmp_path: Path) -> RefreshSaveHeld:
    """ALICE's sign-in, valid for an hour, last renewed past the maximum
    age: the periodic refresh renews it, a reconnect has no refresh due."""
    await make_signed_in_store(tmp_path)
    store = RefreshSaveHeld(tmp_path)
    await seed_sign_in_age(
        store, upstream_id=UPSTREAM_ID, user_id=ALICE, age=timedelta(hours=5),
    )
    return store


async def test_a_forced_refresh_waits_for_the_periodic_refresh_and_connects_with_its_tokens(
    tmp_path: Path,
) -> None:
    wrapper = RotatingRefreshUpstream(refusals=2)
    server, task, url = await serve(wrapper)
    mgr, warner = make_manager(url)
    lock = mgr.sign_in_refresh_lock
    store = await make_store_with_an_aged_sign_in(tmp_path)
    try:
        periodic = await start_periodic_refresh_held_at_its_save(
            store, url, lock, warner,
        )

        # A tool call meets two 401s; its reconnect's forced refresh
        # waits for the periodic refresh.
        call = asyncio.create_task(acquire(mgr, make_upstream(url), store))
        await wait_until(lambda: waiting(lock) == 1)
        store.release.set()
        session = await asyncio.wait_for(call, 30)
        await asyncio.wait_for(periodic, 10)

        result = await session.call_tool("whoami", {})
        assert "at-1" in str(result.content)
        assert wrapper.reused == 0
        assert wrapper.refresh_requests == 1
        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None and stored.refresh_token == "rt-1"
        assert warner.warned == []
    finally:
        store.release.set()
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_forced_refresh_gives_up_on_a_periodic_refresh_that_holds_on(
    tmp_path: Path,
) -> None:
    """The periodic refresh holds the sign-in longer than a reconnect
    waits: the reconnect gives up without refreshing (no refresh token
    presented twice), counts a transient failure and keeps the sign-in,
    which ends up with the periodic refresh's tokens."""
    wrapper = RotatingRefreshUpstream(refusals=2)
    server, task, url = await serve(wrapper)
    mgr, warner = make_manager(url)
    lock = SignInRefreshLock(wait_seconds=0.2)
    mgr.set_sign_in_refresh_lock(lock)
    store = await make_store_with_an_aged_sign_in(tmp_path)
    try:
        periodic = await start_periodic_refresh_held_at_its_save(
            store, url, lock, warner,
        )

        with pytest.raises(Exception):
            await acquire(mgr, make_upstream(url), store)
        store.release.set()
        await asyncio.wait_for(periodic, 10)

        assert wrapper.reused == 0
        assert wrapper.refresh_requests == 1
        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None and stored.refresh_token == "rt-1"
        assert warner.warned == []
    finally:
        store.release.set()
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_reconnect_with_a_refresh_due_waits_for_the_periodic_refresh(
    tmp_path: Path,
) -> None:
    """Both find the bearer due. The reconnect used to refresh the
    refresh token the periodic refresh had just used up."""
    wrapper = RotatingRefreshUpstream(refusals=0)
    server, task, url = await serve(wrapper)
    mgr, warner = make_manager(url)
    lock = mgr.sign_in_refresh_lock
    await make_signed_in_store(
        tmp_path, expires_in=timedelta(seconds=TOKEN_REFRESH_MARGIN / 2),
    )
    store = RefreshSaveHeld(tmp_path)
    try:
        periodic = await start_periodic_refresh_held_at_its_save(
            store, url, lock, warner,
        )

        call = asyncio.create_task(acquire(mgr, make_upstream(url), store))
        await wait_until(lambda: waiting(lock) == 1)
        store.release.set()
        session = await asyncio.wait_for(call, 30)
        await asyncio.wait_for(periodic, 10)

        result = await session.call_tool("whoami", {})
        assert "at-1" in str(result.content)
        assert wrapper.reused == 0
        assert wrapper.refresh_requests == 1
        assert warner.warned == []
    finally:
        store.release.set()
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_shutdown_cancelling_the_periodic_refresh_mid_save_keeps_the_new_tokens(
    tmp_path: Path,
) -> None:
    """The shutdown cancels the periodic refresh's loop. A cancel landing
    after the upstream issued new tokens, retiring the refresh token, and
    before they were saved, left only the retired one stored: the next
    refresh was refused, and the sign-in deleted. The refresh's try now
    finishes its save, then the cancel ends the refresh, with no retry,
    and the lock is let go after the save."""
    wrapper = RotatingRefreshUpstream(refusals=0)
    server, task, url = await serve(wrapper)
    lock = SignInRefreshLock()
    store = await make_store_with_an_aged_sign_in(tmp_path)
    try:
        periodic = await start_periodic_refresh_held_at_its_save(
            store, url, lock, RecordingWarner(),
        )

        periodic.cancel()
        await asyncio.sleep(0.05)
        assert is_held(lock), "the lock was let go before the save"
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(periodic, 10)

        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None and stored.refresh_token == "rt-1"
        assert wrapper.refresh_requests == 1
        assert not is_held(lock)
    finally:
        store.release.set()
        await stop_upstream(server, task)


async def test_the_periodic_refresh_skips_a_sign_in_a_reconnect_is_refreshing(
    tmp_path: Path,
) -> None:
    store = await make_signed_in_store(tmp_path, expires_in=timedelta(minutes=5))
    lock = SignInRefreshLock()

    async with lock.hold(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE):
        with structlog.testing.capture_logs() as logs:
            await refresh_token_for_user(
                DEFAULT_ORG_ID, make_upstream("http://127.0.0.1:9/mcp"), ALICE,
                store, GATEWAY_URL, refresh_lock=lock,
            )

    events = [e["event"] for e in logs]
    assert events == ["oauth.token.refresh.skipped.locked"], events
