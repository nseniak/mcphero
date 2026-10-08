"""Switching to an organization you do not belong to is refused.

The org switcher calls ``POST /api/orgs/{slug}/switch``; on success the
answer carries a new session cookie pointing at that org. For an org the
caller is not a member of, the switch must be refused, the answer must be
the same as for an org that does not exist (so nobody can probe which
org names are taken), and the caller's session must stay on the org it
was on.

Several orgs only exist in cloud mode, so these tests build the real org
routes over the real cloud (Mongo) repositories in a throwaway database,
with orgs created through the real ``OrgService``. The app is driven
in-process through httpx's ASGI transport so the Mongo client and the
app share one event loop. Sign-in is replaced by a session cookie built
the same way sign-in builds it, read back by a minimal stand-in for the
dashboard's "who is signed in" check.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest
from fastapi import Cookie, FastAPI, HTTPException

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    COLL_UPSTREAMS,
    MotorDatabase,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import MongoConfigRepository
from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)
from mcpolis.adapters.repositories.mongo_upstream_config_repository import (
    MongoUpstreamConfigRepository,
)
from mcpolis.adapters.session_revocation_inprocess import (
    InProcessSessionRevocationStore,
)
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.domain.services.org_service import OrgService
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    build_session_cookie,
    get_session_payload,
    is_session_revoked,
)
from mcpolis.entrypoints.routes.org_routes import create_org_router
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database

pytestmark = pytest.mark.skipif(
    not mongo_available(),
    reason="needs Mongo (set MCPOLIS_TEST_MONGO_URI)",
)

ALICE = "alice@acme.test"
BOB = "bob@initech.test"
CAROL = "carol@globex.test"

# The single answer for "no such org" AND "not your org".
REFUSAL_STATUS = 401
REFUSAL_BODY = {"detail": "Not authorized for this org"}


@dataclass
class OrgWorld:
    """Three orgs, seen from Alice's seat.

    - ``acme``: Alice created it; her session starts here.
    - ``initech``: Bob created it and added Alice as a plain member.
    - ``globex``: Carol's org; Alice is not a member.
    """

    settings: Settings
    org_service: OrgService
    upstream_store: MongoUpstreamConfigRepository
    session_revocation: InProcessSessionRevocationStore


def make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        session_secret="org-switch-test-session-secret-0123456789",
    )


def make_org_scoped_collection(db: MotorDatabase, name: str) -> OrgScopedCollection:
    return OrgScopedCollection(
        db[name], name,
        encryptor=FieldEncryptor.from_master_secret("unit-test-key"),
    )


async def make_org_world(db: MotorDatabase) -> OrgWorld:
    config_repo = MongoConfigRepository(make_org_scoped_collection(db, COLL_CONFIG))
    org_repo = MongoOrganizationRepository(db)
    org_service = OrgService(org_repo=org_repo, config_repo=config_repo)
    await org_service.create_organization(
        slug="acme", display_name="Acme", creator_email=ALICE,
    )
    initech = await org_service.create_organization(
        slug="initech", display_name="Initech", creator_email=BOB,
    )
    # What the Team page "Add member" plus Alice's first sign-in do.
    await config_repo.set_user(initech.id, ALICE, UserDefinition(role="user"))
    await org_repo.add_membership(initech.id, ALICE, "user")
    await org_service.create_organization(
        slug="globex", display_name="Globex", creator_email=CAROL,
    )
    return OrgWorld(
        settings=make_settings(),
        org_service=org_service,
        upstream_store=MongoUpstreamConfigRepository(
            make_org_scoped_collection(db, COLL_UPSTREAMS),
            make_org_scoped_collection(db, COLL_CONFIG),
        ),
        session_revocation=InProcessSessionRevocationStore(),
    )


def make_signed_in_user_dependency(
    settings: Settings, session_revocation: InProcessSessionRevocationStore,
) -> Callable[..., Awaitable[str]]:
    """Stand-in for the dashboard's "who is signed in" dependency: the
    email from a valid session cookie that is not deny-listed, else
    401."""

    async def get_current_user(
        mcpolis_session: str | None = Cookie(default=None),
    ) -> str:
        payload = get_session_payload(settings, mcpolis_session)
        email = payload.get("email") if payload is not None else None
        if payload is not None and await is_session_revoked(
            session_revocation, payload,
        ):
            raise HTTPException(status_code=401, detail="Session revoked")
        if not isinstance(email, str):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return email

    return get_current_user


def make_app(world: OrgWorld) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_org_router(
            world.settings,
            world.org_service,
            make_signed_in_user_dependency(
                world.settings, world.session_revocation,
            ),
            upstream_config_store=world.upstream_store,
            session_revocation=world.session_revocation,
        ),
    )
    return app


def make_browser(app: FastAPI, session_cookie: str) -> httpx.AsyncClient:
    """A browser holding ``session_cookie`` as its dashboard session."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    )
    client.cookies.set(COOKIE_NAME, session_cookie)
    return client


def adopt_new_session(client: httpx.AsyncClient, response: httpx.Response) -> None:
    """Do what the browser does with a Set-Cookie: the new session
    replaces the old one."""
    new_cookie = response.cookies[COOKIE_NAME]
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, new_cookie)


async def current_org_slug(client: httpx.AsyncClient) -> str | None:
    """The org the org switcher shows as current."""
    resp = await client.get("/api/orgs")
    assert resp.status_code == 200, resp.text
    current = resp.json()["current_slug"]
    assert current is None or isinstance(current, str)
    return current


async def test_switching_to_an_org_you_belong_to_moves_the_session() -> None:
    """Control case: a member's switch succeeds and the session moves to
    the new org, so the refusals below are a real "no", not a broken
    switch."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        cookie = build_session_cookie(world.settings, email=ALICE, org_slug="acme")
        async with make_browser(app, cookie) as browser:
            assert await current_org_slug(browser) == "acme"

            resp = await browser.post("/api/orgs/initech/switch")

            assert resp.status_code == 204, resp.text
            assert COOKIE_NAME in resp.cookies
            adopt_new_session(browser, resp)
            assert await current_org_slug(browser) == "initech"


async def test_switching_to_an_org_you_do_not_belong_to_is_refused() -> None:
    """Switching to an existing org you are not a member of is refused
    with 401, no new session is handed out, and the session stays on
    the org you were on."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        cookie = build_session_cookie(world.settings, email=ALICE, org_slug="acme")
        async with make_browser(app, cookie) as browser:
            resp = await browser.post("/api/orgs/globex/switch")

            assert resp.status_code == REFUSAL_STATUS, resp.text
            assert resp.json() == REFUSAL_BODY
            assert "set-cookie" not in resp.headers
            assert await current_org_slug(browser) == "acme"


async def test_refusal_for_an_org_you_do_not_belong_to_looks_like_an_unknown_org() -> None:
    """Anti-enumeration: "not your org" gets exactly the same answer as
    "no such org", so the answer cannot reveal which org names exist."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        cookie = build_session_cookie(world.settings, email=ALICE, org_slug="acme")
        async with make_browser(app, cookie) as browser:
            not_a_member = await browser.post("/api/orgs/globex/switch")
            unknown = await browser.post("/api/orgs/no-such-org/switch")

            assert not_a_member.status_code == unknown.status_code == REFUSAL_STATUS
            assert not_a_member.content == unknown.content
            assert "set-cookie" not in unknown.headers
            assert await current_org_slug(browser) == "acme"
