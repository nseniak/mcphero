"""Standalone first sign-in: only one of two simultaneous first
sign-ins may become admin of a fresh install, and one person signing in
from two tabs at once gets no error.

The "no users yet" check that ``_maybe_auto_admin_default_org`` makes
on the running policy can be passed by both sign-ins before either
writes (another settings write holding the store's lock is enough), so
the store must check it again in its own write step
(``add_first_user``)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.routes.dashboard_auth import (
    _maybe_auto_admin_default_org,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.factories import make_runtime_manager


async def make_fresh_install(tmp_path: Path) -> FileConfigStore:
    store = FileConfigStore(tmp_path / "config.json")
    config = await store.ensure_defaults(DEFAULT_ORG_ID)
    assert config.users == {}
    return store


async def sign_in_first_twice_while_the_store_is_busy(
    store: FileConfigStore, first: str, second: str,
) -> list[bool | BaseException]:
    """Both sign-ins pass the running policy's "no users yet" check
    while another write holds the store's lock, then queue on it."""
    config = await store.load(DEFAULT_ORG_ID)
    rm = make_runtime_manager(PolicyEngine(config), org_id=DEFAULT_ORG_ID)
    org_service = AsyncMock()
    lock = store._lock  # pyright: ignore[reportPrivateUsage]
    await lock.acquire()
    tasks = [
        asyncio.ensure_future(
            _maybe_auto_admin_default_org(email, rm, store, org_service),
        )
        for email in (first, second)
    ]
    for _ in range(5):
        await asyncio.sleep(0)
    lock.release()
    return list(await asyncio.gather(*tasks, return_exceptions=True))


@pytest.mark.asyncio
async def test_two_first_sign_ins_at_once_make_one_admin(tmp_path: Path) -> None:
    store = await make_fresh_install(tmp_path)

    results = await sign_in_first_twice_while_the_store_is_busy(
        store, "a@x.com", "b@x.com",
    )

    assert sorted(r for r in results if isinstance(r, bool)) == [False, True]
    assert len((await store.load(DEFAULT_ORG_ID)).users) == 1


@pytest.mark.asyncio
async def test_one_person_signing_in_first_from_two_tabs_gets_no_error(
    tmp_path: Path,
) -> None:
    store = await make_fresh_install(tmp_path)

    results = await sign_in_first_twice_while_the_store_is_busy(
        store, "a@x.com", "A@x.com",
    )

    assert sorted(r for r in results if isinstance(r, bool)) == [False, True]
    assert list((await store.load(DEFAULT_ORG_ID)).users) in (
        ["a@x.com"], ["A@x.com"],
    )
