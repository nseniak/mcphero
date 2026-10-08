"""Finding a person's invitations without reading every org's config.

``GET /api/auth/me`` (every dashboard page load) lists the invitations
of the signed-in person. It used to load the config of every org of the
install, one at a time behind the config store's single process-wide
lock: 200 loads for one person with one invitation in a 200-org install.
The config store now keeps an index of every org's users (members and
invitations), fed by its own reads and writes, which every change of an
org's users goes through, and built once per process from one query.
"""
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
    MotorDatabase,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import (
    MongoConfigRepository,
)
from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)
from mcpolis.domain.model.settings import SettingsConfig, UserDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import (
    ConfigRepository,
    UserAlreadyExistsError,
)
from mcpolis.domain.services.org_service import OrgService
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database

BACKENDS: list[str] = ["file"] + (["mongo"] if mongo_available() else [])
needs_mongo = pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")

INVITEE = "invitee@x.com"
ORG_COUNT = 120


class CountingCollection(OrgScopedCollection):
    """Counts the reads across every org."""

    def __init__(self, db: MotorDatabase) -> None:
        super().__init__(db[COLL_CONFIG], COLL_CONFIG)
        self.cross_org_reads = 0

    async def find_many_cross_org(
        self,
        filter_: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        self.cross_org_reads += 1
        return await super().find_many_cross_org(
            filter_, sort=sort, limit=limit, skip=skip,
        )


class CountingConfigRepository(MongoConfigRepository):
    """Counts the config loads, one org each."""

    def __init__(self, collection: OrgScopedCollection) -> None:
        super().__init__(collection)
        self.loads = 0

    async def load(self, org_id: str) -> SettingsConfig:
        self.loads += 1
        return await super().load(org_id)


@asynccontextmanager
async def make_store(backend: str, tmp_path: Path) -> AsyncIterator[ConfigRepository]:
    """A config store holding the one org ``default``, seeded with the
    default roles."""
    if backend == "file":
        store = FileConfigStore(tmp_path / "config.json")
        await store.ensure_defaults(DEFAULT_ORG_ID)
        yield store
        return
    async with temp_mongo_database() as db:
        mongo_store = MongoConfigRepository(
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG),
        )
        await mongo_store.ensure_defaults(DEFAULT_ORG_ID)
        yield mongo_store


async def roles_of(store: ConfigRepository, email: str) -> dict[str, tuple[str, str]]:
    """org id → (address as stored, role), for every org listing ``email``."""
    return {
        entry.org_id: (entry.email, entry.user.role)
        for entry in await store.find_user(email)
    }


# --- The store's answer follows every change of an org's users ---


@pytest.mark.parametrize("backend", BACKENDS)
async def test_find_user_follows_each_change_of_the_users(
    backend: str, tmp_path: Path,
) -> None:
    async with make_store(backend, tmp_path) as store:
        assert await roles_of(store, INVITEE) == {}

        await store.set_user(DEFAULT_ORG_ID, "Invitee@X.com", UserDefinition(role="user"))
        assert await roles_of(store, INVITEE) == {
            DEFAULT_ORG_ID: ("Invitee@X.com", "user"),
        }

        await store.set_user_role(DEFAULT_ORG_ID, INVITEE, "admin")
        assert await roles_of(store, "INVITEE@x.com") == {
            DEFAULT_ORG_ID: ("Invitee@X.com", "admin"),
        }

        await store.rename_role(DEFAULT_ORG_ID, "admin", "owner")
        assert await roles_of(store, INVITEE) == {
            DEFAULT_ORG_ID: ("Invitee@X.com", "owner"),
        }

        await store.set_user(DEFAULT_ORG_ID, "keeper@x.com", UserDefinition(role="owner"))
        await store.remove_user(DEFAULT_ORG_ID, INVITEE)
        assert await roles_of(store, INVITEE) == {}

        config = await store.load(DEFAULT_ORG_ID)
        config.users[INVITEE] = UserDefinition(role="user")
        await store.save(DEFAULT_ORG_ID, config)
        assert await roles_of(store, INVITEE) == {DEFAULT_ORG_ID: (INVITEE, "user")}

        await store.delete_for_org(DEFAULT_ORG_ID)
        assert await roles_of(store, INVITEE) == {}


@pytest.mark.parametrize("backend", BACKENDS)
async def test_user_writes_act_on_the_stored_spelling(
    backend: str, tmp_path: Path,
) -> None:
    """Letter case is ignored: another spelling changes the one entry,
    and never adds a second one. Adding another spelling is refused
    (``set_user`` only adds; a role changes through ``set_user_role``)."""
    async with make_store(backend, tmp_path) as store:
        await store.set_user(DEFAULT_ORG_ID, "Bob@X.com", UserDefinition(role="user"))
        await store.set_user(DEFAULT_ORG_ID, "root@x.com", UserDefinition(role="admin"))

        with pytest.raises(UserAlreadyExistsError):
            await store.set_user(
                DEFAULT_ORG_ID, "BOB@x.com", UserDefinition(role="admin"),
            )
        await store.set_user_role(DEFAULT_ORG_ID, "bob@X.COM", "admin")
        await store.set_user_role(DEFAULT_ORG_ID, "bob@X.COM", "user")
        assert {
            email: user.role
            for email, user in (await store.load(DEFAULT_ORG_ID)).users.items()
        } == {"Bob@X.com": "user", "root@x.com": "admin"}

        config = await store.remove_user(DEFAULT_ORG_ID, "bob@x.com")
        assert list(config.users) == ["root@x.com"]


@needs_mongo
async def test_find_user_sees_the_users_saved_before_the_store_started() -> None:
    """The index is built from every org's saved config at the first
    lookup (a restart), in one query."""
    async with temp_mongo_database() as db:
        writer = MongoConfigRepository(OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG))
        for org_id in ("org-a", "org-b", "org-c"):
            await writer.ensure_defaults(org_id)
        await writer.set_user("org-a", "Invitee@X.com", UserDefinition(role="user"))
        await writer.set_user("org-c", INVITEE, UserDefinition(role="admin"))

        collection = CountingCollection(db)
        restarted = MongoConfigRepository(collection)

        assert await roles_of(restarted, INVITEE) == {
            "org-a": ("Invitee@X.com", "user"),
            "org-c": (INVITEE, "admin"),
        }
        assert await roles_of(restarted, "someone@else.com") == {}
        assert collection.cross_org_reads == 1


# --- Listing a person's invitations ---


@needs_mongo
async def test_listing_ones_invitations_reads_no_orgs_config() -> None:
    async with temp_mongo_database() as db:
        collection = CountingCollection(db)
        config_repo = CountingConfigRepository(collection)
        org_repo = MongoOrganizationRepository(db)
        service = OrgService(org_repo=org_repo, config_repo=config_repo)
        orgs = [
            await service.create_organization(
                slug=f"org-{i}", display_name=f"Org {i}",
                creator_email=f"owner{i}@x.com",
            )
            for i in range(ORG_COUNT)
        ]
        await config_repo.set_user(orgs[7].id, INVITEE, UserDefinition(role="user"))
        config_repo.loads = 0

        first = await service.list_invitations(INVITEE)
        await config_repo.set_user(
            orgs[42].id, "Invitee@X.com", UserDefinition(role="admin"),
        )
        second = await service.list_invitations(INVITEE)

        assert [(i.org.slug, i.role) for i in first] == [("org-7", "user")]
        assert [(i.org.slug, i.role) for i in second] == [
            ("org-7", "user"), ("org-42", "admin"),
        ]
        assert config_repo.loads == 0
        # Built once, then kept current by the writes.
        assert collection.cross_org_reads <= 1


@needs_mongo
async def test_an_accepted_invitation_is_no_longer_listed() -> None:
    async with temp_mongo_database() as db:
        config_repo = MongoConfigRepository(
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG),
        )
        org_repo = MongoOrganizationRepository(db)
        service = OrgService(org_repo=org_repo, config_repo=config_repo)
        acme = await service.create_organization(
            slug="acme", display_name="Acme", creator_email="owner@acme.com",
        )
        beta = await service.create_organization(
            slug="beta", display_name="Beta", creator_email="owner@beta.com",
        )
        await config_repo.set_user(acme.id, "Invitee@X.com", UserDefinition(role="user"))
        await config_repo.set_user(beta.id, INVITEE, UserDefinition(role="user"))

        await org_repo.add_membership(acme.id, INVITEE, "user")

        assert [i.org.slug for i in await service.list_invitations(INVITEE)] == ["beta"]
        assert await service.list_invitations("owner@acme.com") == []

        await service.delete_organization(beta.id)

        assert await service.list_invitations(INVITEE) == []


class FirstReadStallsCollection(OrgScopedCollection):
    """The first ``find_one`` after ``stall_next_read`` reads the
    document, then waits for ``release`` before handing it back: a slow
    read that finishes after a later write."""

    def __init__(self, raw: Any) -> None:
        super().__init__(raw, COLL_CONFIG)
        self.release = asyncio.Event()
        self._stall = False

    def stall_next_read(self) -> None:
        self._stall = True

    async def find_one(
        self, org_id: str, filter_: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        doc = await super().find_one(org_id, filter_)
        if self._stall:
            self._stall = False
            await self.release.wait()
        return doc


@needs_mongo
async def test_a_slow_load_cannot_hide_an_invitation_saved_after_it() -> None:
    """A dashboard load that read the org before an invitation landed
    must not put the older user list back into the index ``find_user``
    answers from, or the invitation disappears from the invited
    person's list until the next read of that org."""
    async with temp_mongo_database() as db:
        coll = FirstReadStallsCollection(db[COLL_CONFIG])
        repo = MongoConfigRepository(coll)
        await repo.ensure_defaults("org-a")
        await repo.find_user("nobody@x.com")  # builds the index
        coll.stall_next_read()
        load = asyncio.ensure_future(repo.load("org-a"))
        await asyncio.sleep(0.05)
        invite = asyncio.ensure_future(
            repo.set_user("org-a", INVITEE, UserDefinition(role="user")),
        )
        await asyncio.sleep(0.2)

        coll.release.set()
        await asyncio.gather(load, invite)

        assert [entry.email for entry in await repo.find_user(INVITEE)] == [INVITEE]
