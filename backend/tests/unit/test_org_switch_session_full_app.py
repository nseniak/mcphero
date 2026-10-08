"""Org switch and sign-out, through the whole app.

The switch tests in ``test_org_switch_session_lifetime`` drive the org
routes alone, with a stand-in for "who is signed in". These drive the
real app (standalone, dev-stub sign-in), so the real deny-list check
and the deny-list the app wires into the org routes are the ones under
test.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mcpolis.entrypoints.routes.dashboard_auth import COOKIE_NAME
from tests.unit._dev_stub_login import login_as
from tests.unit.test_dashboard_api import make_test_client

ADMIN = "admin@example.com"

# Routes behind each of the three "who is signed in" checks.
SIGNED_IN_PATHS = ("/api/auth/me", "/api/orgs", "/api/admin/users")


def current_cookie(client: TestClient) -> str:
    value = client.cookies.get(COOKIE_NAME)
    assert value
    return value


def hold(client: TestClient, cookie: str) -> None:
    """Make ``client`` a browser holding ``cookie`` only."""
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, cookie)


def switch(client: TestClient, cookie: str) -> str:
    """Switch org with ``cookie``; the cookie the answer hands out."""
    hold(client, cookie)
    resp = client.post("/api/orgs/default/switch")
    assert resp.status_code == 204, resp.text
    new_cookie = resp.cookies.get(COOKIE_NAME)
    assert new_cookie and new_cookie != cookie
    return new_cookie


def works(client: TestClient, cookie: str) -> bool:
    hold(client, cookie)
    codes = {client.get(path).status_code for path in SIGNED_IN_PATHS}
    assert codes in ({200}, {401}), codes
    return codes == {200}


def test_the_cookie_from_before_the_switch_stops_working(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    old = current_cookie(client)

    new = switch(client, old)

    assert not works(client, old)
    hold(client, old)
    assert client.post("/api/orgs/default/switch").status_code == 401
    assert works(client, new)


def test_sign_out_ends_every_cookie_an_org_switch_made(tmp_path: Path) -> None:
    """Someone holding a copy switches it, which ends the owner's cookie
    and gives the copier a cookie of the same sign-in. The owner's
    sign-out, even with the ended cookie, ends the copier's too."""
    client = make_test_client(tmp_path)
    owners = current_cookie(client)
    copiers = switch(client, owners)
    copiers_next = switch(client, copiers)
    assert works(client, copiers_next)

    hold(client, owners)
    assert client.post("/api/auth/logout").status_code == 204

    assert not works(client, copiers_next)


def test_sign_out_leaves_another_sign_in_working(tmp_path: Path) -> None:
    """Ending one Google sign-in leaves the same person's sign-in on
    another device alone."""
    client = make_test_client(tmp_path)
    this_device = switch(client, current_cookie(client))
    client.cookies.clear()
    login_as(client, ADMIN)
    other_device = current_cookie(client)

    hold(client, this_device)
    assert client.post("/api/auth/logout").status_code == 204

    assert not works(client, this_device)
    assert works(client, other_device)
