"""Signing in to the dashboard from an org's join link, in cloud mode.

Where the sign-in lands depends on who signs in:

- a stranger to the org (neither a member nor invited) goes back to the
  org's Join page with ``auth_error=not_a_member``, signed in nowhere, so
  the page can tell them to ask for an invitation;
- an invited person lands on the Join page, signed in, to accept it,
  whatever the letter case the invitation was typed with;
- a member lands on the dashboard of that org.

Only the cloud callback reaches the join-link branch: a standalone
stranger is turned away before it.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.domain.model.settings import (
    RoleDefinition,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.ports.dashboard_oauth_provider import CompletedLogin
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    create_dashboard_auth,
)
from tests.unit.factories import make_runtime_manager
from tests.unit.test_multi_org_gateway import (
    InMemoryOrgRepo,
    make_membership,
    make_org,
)

BASE_URL = "http://localhost:8000"
ACME_ID, ACME = "acme-id", "acme"
MEMBER = "member@acme.test"
INVITEE = "invitee@acme.test"
STRANGER = "stranger@elsewhere.test"


class SignsInAs:
    """A dashboard sign-in provider whose consent screen always signs in
    ``email``. Keeps each login's state, as the browser would carry it
    back to the callback."""

    name = "test"

    def __init__(self, email: str) -> None:
        self.email = email
        self.states: list[str] = []

    async def start_login(
        self, *, state: str, redirect_uri: str, join: str | None,
    ) -> str:
        del redirect_uri, join
        self.states.append(state)
        return "https://accounts.example/consent"

    async def complete_login(
        self, *, code: str, state: str, redirect_uri: str,
    ) -> CompletedLogin:
        del code, state, redirect_uri
        return CompletedLogin(email=self.email)


def make_dashboard(tmp_path: Path, provider: SignsInAs) -> httpx.AsyncClient:
    """acme's dashboard sign-in: ``MEMBER`` is a member, ``Invitee@...``
    is invited (typed with capitals)."""
    config_path = tmp_path / "config.json"
    config_path.write_text(SettingsConfig(
        roles={"user": RoleDefinition(is_default=True)},
        users={
            MEMBER: UserDefinition(role="user"),
            "Invitee@Acme.test": UserDefinition(role="user"),
        },
    ).model_dump_json())
    config_store = FileConfigStore(config_path)
    org_repo = InMemoryOrgRepo(
        orgs=[make_org(ACME_ID, ACME, "Acme")],
        memberships=[make_membership(ACME_ID, MEMBER, "user")],
    )
    auth = create_dashboard_auth(
        Settings(_env_file=None, mode="cloud", server_url=BASE_URL),  # type: ignore[call-arg]
        make_runtime_manager(PolicyEngine(SettingsConfig()), org_id=ACME_ID),
        config_store,
        OrgService(org_repo=org_repo, config_repo=config_store),  # type: ignore[arg-type]
        provider,
    )
    app = FastAPI()
    app.include_router(auth.router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
    )


async def sign_in_from_join_link(
    tmp_path: Path, email: str,
) -> httpx.Response:
    """``email`` clicks acme's join link and signs in: the callback's
    answer."""
    provider = SignsInAs(email)
    async with make_dashboard(tmp_path, provider) as client:
        login = await client.get("/api/auth/login", params={"join": ACME})
        assert login.status_code == 307, login.text
        return await client.get(
            "/api/auth/callback",
            params={"code": "consent-code", "state": provider.states[-1]},
        )


async def test_a_stranger_from_a_join_link_is_told_they_are_not_a_member(
    tmp_path: Path,
) -> None:
    callback = await sign_in_from_join_link(tmp_path, STRANGER)

    assert callback.status_code == 302, callback.text
    landing = urlparse(callback.headers["location"])
    assert landing.path == f"/orgs/{ACME}/join"
    assert parse_qs(landing.query) == {
        "auth_error": ["not_a_member"], "email": [STRANGER], "org": [ACME],
    }
    assert COOKIE_NAME not in callback.cookies


async def test_an_invited_person_from_a_join_link_lands_on_the_join_page(
    tmp_path: Path,
) -> None:
    """Invited as ``Invitee@Acme.test``, signed in as ``invitee@...``."""
    callback = await sign_in_from_join_link(tmp_path, INVITEE)

    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == f"/orgs/{ACME}/join"
    assert COOKIE_NAME in callback.cookies


async def test_a_member_from_a_join_link_lands_on_the_dashboard(
    tmp_path: Path,
) -> None:
    callback = await sign_in_from_join_link(tmp_path, MEMBER)

    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == "/"
    assert COOKIE_NAME in callback.cookies
