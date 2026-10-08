"""Parameterized ``ConfigRepository`` tests."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import (
    MongoConfigRepository,
)
from mcpolis.domain.model.settings import (
    DEFAULT_SETTINGS_CONFIG,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import (
    ConfigRepository,
    ConfigWriteConflictError,
    UserAlreadyExistsError,
)
from mcpolis.domain.services.settings_resolver import LastAdminError
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


BACKENDS: list[str] = ["file"] + (["mongo"] if mongo_available() else [])


@asynccontextmanager
async def _make_store(backend: str, tmp_path: Path) -> AsyncIterator[ConfigRepository]:
    if backend == "file":
        store = FileConfigStore(tmp_path / "config.json")
        await store.ensure_defaults(DEFAULT_ORG_ID)
        yield store
        return
    async with temp_mongo_database() as db:
        coll = OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG)
        store2 = MongoConfigRepository(coll)
        await store2.ensure_defaults(DEFAULT_ORG_ID)
        yield store2


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_defaults_seeded(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        config = await store.load(DEFAULT_ORG_ID)
        assert "admin" in config.roles
        assert "user" in config.roles


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_and_remove_user(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        config = await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin")
        )
        assert "alice@test.com" in config.users
        assert config.users["alice@test.com"].role == "admin"
        # A second admin, because the store now refuses to remove the
        # last one. This test is about the set/remove round trip, not
        # about that invariant (which has its own tests below).
        await store.set_user(
            DEFAULT_ORG_ID, "root@test.com", UserDefinition(role="admin")
        )
        config = await store.remove_user(DEFAULT_ORG_ID, "alice@test.com")
        assert "alice@test.com" not in config.users


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_user_refuses_a_role_that_does_not_exist(
    backend: str, tmp_path: Path,
) -> None:
    """Checked under the store's lock: a caller's earlier check can be
    stale if the role was renamed or deleted since."""
    async with _make_store(backend, tmp_path) as store:
        with pytest.raises(ValueError, match="Role 'ghost' not found"):
            await store.set_user(
                DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="ghost"),
            )
        config = await store.load(DEFAULT_ORG_ID)
        assert "alice@test.com" not in config.users


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_create_and_delete_role(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        config = await store.create_role(DEFAULT_ORG_ID, "operator")
        assert "operator" in config.roles
        config = await store.delete_role(DEFAULT_ORG_ID, "operator")
        assert "operator" not in config.roles


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_last_role_raises(backend: str, tmp_path: Path) -> None:
    """An org must keep at least one role: a zero-roles org denies
    every identity (PolicyEngine fails closed), so the store refuses
    to delete the last one."""
    async with _make_store(backend, tmp_path) as store:
        # Defaults seed "admin" + "user" with no users assigned;
        # deleting one is fine, deleting the survivor is not.
        config = await store.delete_role(DEFAULT_ORG_ID, "user")
        assert set(config.roles) == {"admin"}
        with pytest.raises(ValueError, match="at least one role"):
            await store.delete_role(DEFAULT_ORG_ID, "admin")
        config = await store.load(DEFAULT_ORG_ID)
        assert set(config.roles) == {"admin"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_cross_org_isolation(backend: str, tmp_path: Path) -> None:
    """org_a's writes must not leak into org_b's reads."""
    async with _make_store(backend, tmp_path) as store:
        # File store is single-org by design, so only test cross-org
        # isolation on the Mongo backend.
        if backend == "file":
            pytest.skip("file store is single-default-org by design")
        # Same repo, different orgs — only Mongo actually partitions.
        other_org = "other-org"
        await store.ensure_defaults(other_org)
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin")
        )
        other = await store.load(other_org)
        assert "alice@test.com" not in other.users


# --- Last-admin invariant, enforced in the store ----------------------
# The route-level pre-check cannot hold this on its own: it reads the
# config, awaits, then writes, so two parallel calls each see a
# surviving admin and both proceed. The store does the check inside the
# same lock as the write, which is what actually makes it safe.


async def _seed_two_admins(store: ConfigRepository) -> None:
    for email in ("alice@test.com", "bob@test.com"):
        await store.set_user(DEFAULT_ORG_ID, email, UserDefinition(role="admin"))


def _admins(config: SettingsConfig) -> set[str]:
    return {
        e for e, u in config.users.items()
        if (r := config.roles.get(u.role)) is not None and r.is_admin
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_store_refuses_to_remove_the_only_admin(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin"))
        with pytest.raises(LastAdminError):
            await store.remove_user(DEFAULT_ORG_ID, "alice@test.com")
        config = await store.load(DEFAULT_ORG_ID)
        assert _admins(config) == {"alice@test.com"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_store_refuses_to_demote_the_only_admin(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin"))
        with pytest.raises(LastAdminError):
            await store.set_user_role(
                DEFAULT_ORG_ID, "alice@test.com", "user")
        config = await store.load(DEFAULT_ORG_ID)
        assert _admins(config) == {"alice@test.com"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_parallel_removals_cannot_empty_the_admins(
    backend: str, tmp_path: Path,
) -> None:
    """Two admins, two concurrent removals, one for each.

    Both callers see two admins when they start. Exactly one may
    succeed. Before the store-level check this left zero admins on
    Mongo, which is production.
    """
    async with _make_store(backend, tmp_path) as store:
        await _seed_two_admins(store)

        results = await asyncio.gather(
            store.remove_user(DEFAULT_ORG_ID, "alice@test.com"),
            store.remove_user(DEFAULT_ORG_ID, "bob@test.com"),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, LastAdminError)]
        assert len(refused) == 1, f"expected exactly one refusal, got {results}"

        config = await store.load(DEFAULT_ORG_ID)
        assert len(_admins(config)) == 1, (
            f"org ended with {_admins(config)} admins; the invariant is that "
            f"at least one always survives"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_parallel_demotions_cannot_empty_the_admins(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        await _seed_two_admins(store)

        results = await asyncio.gather(
            store.set_user_role(DEFAULT_ORG_ID, "alice@test.com", "user"),
            store.set_user_role(DEFAULT_ORG_ID, "bob@test.com", "user"),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, LastAdminError)]
        assert len(refused) == 1, f"expected exactly one refusal, got {results}"

        config = await store.load(DEFAULT_ORG_ID)
        assert len(_admins(config)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_removing_one_of_two_admins_still_works(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        await _seed_two_admins(store)
        config = await store.remove_user(DEFAULT_ORG_ID, "alice@test.com")
        assert _admins(config) == {"bob@test.com"}


# --- One person under two spellings ------------------------------------
# An org saved before addresses compared ignoring letter case may hold
# ``bob@test.com`` and ``Bob@Test.com``. A removal or a role change acts
# on both; a spelling left behind reads as a pending invitation the
# removed person could accept again.


async def _seed_users(store: ConfigRepository, users: dict[str, str]) -> None:
    """Saved as is (``set_user`` would merge the spellings): email → role."""
    config = await store.load(DEFAULT_ORG_ID)
    for email, role in users.items():
        config.users[email] = UserDefinition(role=role)
    await store.save(DEFAULT_ORG_ID, config)


def _roles(config: SettingsConfig) -> dict[str, str]:
    return {email: user.role for email, user in config.users.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_removal_removes_every_spelling(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        await _seed_users(store, {
            "root@test.com": "admin", "bob@test.com": "user", "Bob@Test.com": "user",
        })

        config = await store.remove_user(DEFAULT_ORG_ID, "BOB@test.com")

        assert _roles(config) == {"root@test.com": "admin"}
        assert _roles(await store.load(DEFAULT_ORG_ID)) == {"root@test.com": "admin"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_role_change_reaches_every_spelling(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        await _seed_users(store, {
            "root@test.com": "admin", "bob@test.com": "user", "Bob@Test.com": "user",
        })

        config = await store.set_user_role(DEFAULT_ORG_ID, "bob@test.com", "admin")

        expected = {
            "root@test.com": "admin", "bob@test.com": "admin", "Bob@Test.com": "admin",
        }
        assert _roles(config) == expected
        assert _roles(await store.load(DEFAULT_ORG_ID)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_the_only_admin_under_two_spellings_is_kept(
    backend: str, tmp_path: Path,
) -> None:
    """One person, so one admin: neither removed nor demoted."""
    async with _make_store(backend, tmp_path) as store:
        users = {
            "bob@test.com": "admin", "Bob@Test.com": "admin", "dev@test.com": "user",
        }
        await _seed_users(store, users)

        with pytest.raises(LastAdminError):
            await store.remove_user(DEFAULT_ORG_ID, "bob@test.com")
        with pytest.raises(LastAdminError):
            await store.set_user_role(DEFAULT_ORG_ID, "Bob@Test.com", "user")

        assert _roles(await store.load(DEFAULT_ORG_ID)) == users
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_parallel_removals_cannot_leave_only_an_invited_admin(
    backend: str, tmp_path: Path,
) -> None:
    """Two signed-in admins plus one invited admin who never signed in.

    The routes pre-check against signed-in admins only; the store's
    re-check must count the same set. Otherwise both parallel removals
    pass (the store sees the invited address as a surviving admin) and
    the org is left with an admin nobody can sign in as.
    """
    async with _make_store(backend, tmp_path) as store:
        await _seed_two_admins(store)
        await store.set_user(
            DEFAULT_ORG_ID, "invited@test.com", UserDefinition(role="admin"))
        signed_in = {"alice@test.com", "bob@test.com"}

        results = await asyncio.gather(
            store.remove_user(
                DEFAULT_ORG_ID, "alice@test.com", eligible=signed_in),
            store.remove_user(
                DEFAULT_ORG_ID, "bob@test.com", eligible=signed_in),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, LastAdminError)]
        assert len(refused) == 1, f"expected exactly one refusal, got {results}"

        config = await store.load(DEFAULT_ORG_ID)
        assert _admins(config) & signed_in


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_demoting_last_signed_in_admin_refused_despite_invited_admin(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin"))
        await store.set_user(
            DEFAULT_ORG_ID, "invited@test.com", UserDefinition(role="admin"))

        with pytest.raises(LastAdminError):
            await store.set_user_role(
                DEFAULT_ORG_ID, "alice@test.com", "user",
                eligible={"alice@test.com"},
            )


class _ReadsBeforeWritesCollection(OrgScopedCollection):
    """Holds each backend's FIRST read until both backends have read,
    so both see the same document before either writes: the ordering
    network delay produces between two real backend processes."""

    def __init__(self, raw: Any, both_read: asyncio.Barrier) -> None:
        super().__init__(raw, COLL_CONFIG)
        self._both_read = both_read
        self._held = False

    async def find_one(
        self, org_id: str, filter_: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        doc = await super().find_one(org_id, filter_)
        if not self._held:
            self._held = True
            await self._both_read.wait()
        return doc


def make_two_backend_repos(
    db: Any,
) -> tuple[MongoConfigRepository, MongoConfigRepository]:
    both_read = asyncio.Barrier(2)
    return (
        MongoConfigRepository(_ReadsBeforeWritesCollection(db[COLL_CONFIG], both_read)),
        MongoConfigRepository(_ReadsBeforeWritesCollection(db[COLL_CONFIG], both_read)),
    )


@pytest.mark.asyncio
async def test_two_backend_processes_cannot_empty_the_admins() -> None:
    """Two backend processes = two repository objects over one Mongo
    collection, each with its own in-process lock. Parallel removal of
    the two admins, one through each: exactly one may succeed."""
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        setup = MongoConfigRepository(OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG))
        await setup.ensure_defaults(DEFAULT_ORG_ID)
        await _seed_two_admins(setup)
        first, second = make_two_backend_repos(db)

        results = await asyncio.gather(
            first.remove_user(DEFAULT_ORG_ID, "alice@test.com"),
            second.remove_user(DEFAULT_ORG_ID, "bob@test.com"),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, LastAdminError)]
        assert len(refused) == 1, f"expected exactly one refusal, got {results}"
        config = await setup.load(DEFAULT_ORG_ID)
        assert len(_admins(config)) == 1


@pytest.mark.asyncio
async def test_two_backend_processes_keep_both_changes() -> None:
    """Two unrelated changes racing from two processes: neither is lost
    (a blind overwrite would drop the first one written)."""
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        setup = MongoConfigRepository(OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG))
        await setup.ensure_defaults(DEFAULT_ORG_ID)
        first, second = make_two_backend_repos(db)

        await asyncio.gather(
            first.create_role(DEFAULT_ORG_ID, "ops"),
            second.set_user(
                DEFAULT_ORG_ID, "carol@test.com", UserDefinition(role="user")),
        )

        config = await setup.load(DEFAULT_ORG_ID)
        assert "ops" in config.roles
        assert "carol@test.com" in config.users


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_user_refuses_an_address_that_is_already_a_user(
    backend: str, tmp_path: Path,
) -> None:
    """``set_user`` adds a user and must not overwrite one. Every caller
    adds a new address; an overwrite would change a role with no
    last-admin check, for example demoting the only admin."""
    async with _make_store(backend, tmp_path) as store:
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin"))

        with pytest.raises(UserAlreadyExistsError):
            await store.set_user(
                DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="user"))

        assert _admins(await store.load(DEFAULT_ORG_ID)) == {"alice@test.com"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_user_refuses_another_spelling_of_a_user(
    backend: str, tmp_path: Path,
) -> None:
    """Addresses compare ignoring letter case, so ``Alice@Test.com``
    is the same person as ``alice@test.com``: adding it would overwrite
    her role with no last-admin check."""
    async with _make_store(backend, tmp_path) as store:
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin"))

        with pytest.raises(UserAlreadyExistsError):
            await store.set_user(
                DEFAULT_ORG_ID, "Alice@Test.com", UserDefinition(role="user"))

        assert _roles(await store.load(DEFAULT_ORG_ID)) == {
            "alice@test.com": "admin",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_add_first_user_adds_only_to_an_org_with_no_users(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        first = await store.add_first_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin"))
        second = await store.add_first_user(
            DEFAULT_ORG_ID, "bob@test.com", UserDefinition(role="admin"))

        assert first is not None and second is None
        assert _roles(await store.load(DEFAULT_ORG_ID)) == {
            "alice@test.com": "admin",
        }


def make_mongo_repo(db: Any) -> MongoConfigRepository:
    return MongoConfigRepository(OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG))


@pytest.mark.asyncio
@pytest.mark.parametrize("rev", [3.0, None, "hand-edited"])
async def test_hand_edited_revision_does_not_lock_the_settings(rev: Any) -> None:
    """Fixing an org by hand in the database is the documented recovery
    for an org with no admin. A ``rev`` stored as a double, a null or
    any other value must not make every later write fail."""
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        repo = make_mongo_repo(db)
        await repo.ensure_defaults(DEFAULT_ORG_ID)
        await db[COLL_CONFIG].update_one(
            {"org_id": DEFAULT_ORG_ID}, {"$set": {"rev": rev}})

        await repo.set_user(
            DEFAULT_ORG_ID, "carol@test.com", UserDefinition(role="admin"))

        assert "carol@test.com" in (await repo.load(DEFAULT_ORG_ID)).users
        doc = await db[COLL_CONFIG].find_one({"org_id": DEFAULT_ORG_ID})
        assert doc is not None and isinstance(doc["rev"], int)


class _AlwaysOutracedCollection(OrgScopedCollection):
    """Every conditional write finds the document changed, as if another
    writer always committed first."""

    async def replace_one(
        self,
        org_id: str,
        filter_: dict[str, Any],
        doc: dict[str, Any],
        *,
        upsert: bool = True,
    ) -> int:
        return 0


@pytest.mark.asyncio
async def test_write_that_keeps_losing_gives_up_and_writes_nothing() -> None:
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        await make_mongo_repo(db).ensure_defaults(DEFAULT_ORG_ID)
        outraced = MongoConfigRepository(
            _AlwaysOutracedCollection(db[COLL_CONFIG], COLL_CONFIG))

        with pytest.raises(ConfigWriteConflictError) as refused:
            await outraced.set_user(
                DEFAULT_ORG_ID, "carol@test.com", UserDefinition(role="user"))
        # The Admin MCP shows this text to the AI client as is.
        assert DEFAULT_ORG_ID not in str(refused.value)
        assert "try again" in str(refused.value).lower()

        users = (await make_mongo_repo(db).load(DEFAULT_ORG_ID)).users
        assert "carol@test.com" not in users


@pytest.mark.asyncio
async def test_settings_saved_before_revisions_keep_both_racing_changes() -> None:
    """A settings document written before revisions existed has no
    ``rev``. Two backends racing on it must both land."""
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        await db[COLL_CONFIG].insert_one({
            "org_id": DEFAULT_ORG_ID,
            "config": DEFAULT_SETTINGS_CONFIG.model_dump(mode="json"),
        })
        first, second = make_two_backend_repos(db)

        await asyncio.gather(
            first.create_role(DEFAULT_ORG_ID, "ops"),
            second.set_user(
                DEFAULT_ORG_ID, "carol@test.com", UserDefinition(role="user")),
        )

        config = await make_mongo_repo(db).load(DEFAULT_ORG_ID)
        assert "ops" in config.roles
        assert "carol@test.com" in config.users
        doc = await db[COLL_CONFIG].find_one({"org_id": DEFAULT_ORG_ID})
        assert doc is not None and doc["rev"] == 2


@pytest.mark.asyncio
async def test_new_org_two_backends_writing_first_share_one_document() -> None:
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        first, second = make_two_backend_repos(db)

        await asyncio.gather(
            first.create_role(DEFAULT_ORG_ID, "ops"),
            second.create_role(DEFAULT_ORG_ID, "dev"),
        )

        assert await db[COLL_CONFIG].count_documents(
            {"org_id": DEFAULT_ORG_ID}) == 1
        roles = (await make_mongo_repo(db).load(DEFAULT_ORG_ID)).roles
        assert set(roles) == {"ops", "dev"}
