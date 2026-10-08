"""Switching org must not make a dashboard sign-in last longer.

A Google sign-in lasts ``SESSION_TTL`` (7 days); after that the person
must sign in with Google again. ``POST /api/orgs/{slug}/switch`` hands
out a new session cookie pointing at the new org. That new cookie must
end when the original sign-in ends, or switching org once a week would
keep a session (or a stolen cookie) alive for ever, without Google. The
cookie it replaces is deny-listed, so a copy of it stops working and
signing out of the new cookie ends the whole sign-in.

The org-switch tests reuse the real org routes over Mongo from
``test_org_switch_non_member``.
"""
from __future__ import annotations

import time
from http.cookies import SimpleCookie
from typing import Any

import httpx
import pytest

from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    SESSION_TTL,
    _sign_cookie,
    build_session_cookie,
    build_switched_session_cookie,
    get_session_payload,
    get_signing_key,
)
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database
from tests.unit.test_org_switch_non_member import (
    ALICE,
    adopt_new_session,
    make_app,
    make_browser,
    make_org_world,
    make_settings,
)

pytestmark = pytest.mark.skipif(
    not mongo_available(),
    reason="needs Mongo (set MCPOLIS_TEST_MONGO_URI)",
)


def make_cookie_signed_in_at(
    settings: Settings, signed_in_at: float, org_slug: str,
) -> str:
    """A session cookie exactly as a Google sign-in at ``signed_in_at``
    would have built it."""
    return make_signed_cookie(
        settings,
        {
            "email": ALICE,
            "org_slug": org_slug,
            "jti": "original-sign-in",
            "iat": signed_in_at,
            "exp": signed_in_at + SESSION_TTL,
        },
    )


def make_signed_cookie(settings: Settings, payload: dict[str, Any]) -> str:
    return _sign_cookie(payload, get_signing_key(settings))


def decode(settings: Settings, cookie_value: str) -> dict[str, Any]:
    payload = get_session_payload(settings, cookie_value)
    assert payload is not None
    return payload


def browser_keeps_cookie_for(response: httpx.Response) -> int:
    """The ``Max-Age`` the answer's Set-Cookie gives the session cookie."""
    parsed: SimpleCookie = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    return int(parsed[COOKIE_NAME]["max-age"])


async def test_switching_org_keeps_the_end_of_the_original_sign_in() -> None:
    """A cookie one minute from its end, switched to another org, still
    ends one minute from now, and the browser is told to drop it then."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        signed_in_at = time.time() - SESSION_TTL + 60
        cookie = make_cookie_signed_in_at(world.settings, signed_in_at, "acme")
        async with make_browser(app, cookie) as browser:
            resp = await browser.post("/api/orgs/initech/switch")

        assert resp.status_code == 204, resp.text
        new_payload = decode(world.settings, resp.cookies[COOKIE_NAME])
        assert new_payload["org_slug"] == "initech"
        assert new_payload["email"] == ALICE
        assert new_payload["iat"] == signed_in_at
        assert new_payload["exp"] == signed_in_at + SESSION_TTL
        assert new_payload["jti"] != "original-sign-in"
        assert 0 < browser_keeps_cookie_for(resp) <= 60


async def test_switching_org_again_and_again_never_moves_the_end() -> None:
    """Switching back and forth, each time with the newest cookie, keeps
    the end the Google sign-in set."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        signed_in_at = time.time() - 3600
        cookie = make_cookie_signed_in_at(world.settings, signed_in_at, "acme")
        async with make_browser(app, cookie) as browser:
            for slug in ("initech", "acme", "initech"):
                resp = await browser.post(f"/api/orgs/{slug}/switch")
                assert resp.status_code == 204, resp.text
                adopt_new_session(browser, resp)
                payload = decode(world.settings, resp.cookies[COOKIE_NAME])
                assert payload["exp"] == signed_in_at + SESSION_TTL


async def test_the_cookie_from_before_the_switch_stops_working() -> None:
    """A copy of the pre-switch cookie (another device, a thief) is
    refused after the switch; the new cookie works."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        old_cookie = build_session_cookie(
            world.settings, email=ALICE, org_slug="acme",
        )
        async with make_browser(app, old_cookie) as browser:
            resp = await browser.post("/api/orgs/initech/switch")
            assert resp.status_code == 204, resp.text
            new_cookie = resp.cookies[COOKIE_NAME]

        async with make_browser(app, old_cookie) as copy_holder:
            assert (await copy_holder.get("/api/orgs")).status_code == 401
            replay = await copy_holder.post("/api/orgs/acme/switch")
            assert replay.status_code == 401
            assert "set-cookie" not in replay.headers

        async with make_browser(app, new_cookie) as browser:
            assert (await browser.get("/api/orgs")).status_code == 200


async def test_a_refused_switch_leaves_the_current_cookie_working() -> None:
    """A switch to an org the caller is not in deny-lists nothing."""
    async with temp_mongo_database() as db:
        world = await make_org_world(db)
        app = make_app(world)
        cookie = build_session_cookie(world.settings, email=ALICE, org_slug="acme")
        async with make_browser(app, cookie) as browser:
            resp = await browser.post("/api/orgs/globex/switch")
            assert resp.status_code == 401
            assert (await browser.get("/api/orgs")).status_code == 200


def test_a_google_sign_in_still_lasts_seven_days() -> None:
    settings = make_settings()
    before = time.time()
    payload = decode(
        settings, build_session_cookie(settings, email=ALICE, org_slug="acme"),
    )
    assert before <= payload["iat"] <= time.time()
    assert payload["exp"] == payload["iat"] + SESSION_TTL


def test_switched_cookie_never_ends_later_than_seven_days_after_sign_in() -> None:
    """An end beyond sign-in + SESSION_TTL (a cookie from a time when the
    limit was longer) is cut back to the current limit."""
    settings = make_settings()
    signed_in_at = time.time() - 60
    switched = build_switched_session_cookie(
        settings,
        {
            "email": ALICE,
            "org_slug": "acme",
            "jti": "old",
            "iat": signed_in_at,
            "exp": signed_in_at + 10 * SESSION_TTL,
        },
        org_slug="initech",
    )
    assert switched is not None
    assert decode(settings, switched)["exp"] == signed_in_at + SESSION_TTL


def test_switched_cookie_is_refused_without_a_sign_in_time() -> None:
    """A payload missing its sign-in time or end gives no new cookie
    (never a fresh 7 days)."""
    settings = make_settings()
    now = time.time()
    base: dict[str, Any] = {
        "email": ALICE, "org_slug": "acme", "jti": "old",
        "iat": now, "exp": now + 60,
    }
    for missing in ("iat", "exp", "email"):
        payload = {k: v for k, v in base.items() if k != missing}
        assert build_switched_session_cookie(settings, payload, "initech") is None
    assert build_switched_session_cookie(
        settings, {**base, "iat": True}, "initech",
    ) is None


def test_a_cookie_without_an_end_is_refused() -> None:
    """The token format accepts a payload with no ``exp`` for ever; a
    session cookie must not."""
    settings = make_settings()
    now = time.time()
    cookie = make_signed_cookie(
        settings,
        {"email": ALICE, "org_slug": "acme", "jti": "j", "sid": "s", "iat": now},
    )
    assert get_session_payload(settings, cookie) is None


def test_switched_cookie_keeps_the_sign_in_id() -> None:
    """Every cookie of one Google sign-in shares its ``sid``, so a
    sign-out can end them all; a cookie from before ``sid`` existed
    counts its ``jti`` as the sign-in id."""
    settings = make_settings()
    signed_in = decode(
        settings, build_session_cookie(settings, email=ALICE, org_slug="acme"),
    )
    switched = build_switched_session_cookie(settings, signed_in, "initech")
    assert switched is not None
    assert decode(settings, switched)["sid"] == signed_in["sid"]

    now = time.time()
    no_sid = {"email": ALICE, "org_slug": "acme", "jti": "old-jti",
              "iat": now, "exp": now + 60}
    from_old = build_switched_session_cookie(settings, no_sid, "initech")
    assert from_old is not None
    assert decode(settings, from_old)["sid"] == "old-jti"
