"""Shared helpers for unit tests that exercise the dashboard cookie path.

Phase C migrated every previously-header-based test off the legacy
``oauth_enabled=False`` path. After this, hitting an authenticated
endpoint requires logging in through the dev-stub provider first —
same code path as production, just with the Google round-trip
swapped for an in-app email picker.

Usage:

    client = make_test_client(tmp_path)
    login_as(client, "admin@example.com")
    resp = client.get("/api/admin/upstreams")
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def login_as(client: TestClient, email: str) -> None:
    """Walk the dev-stub /login → /submit → /callback flow, leaving the
    signed session cookie in the TestClient's cookie jar."""
    login_resp = client.get("/api/auth/login", follow_redirects=False)
    assert login_resp.status_code == 307, login_resp.text
    location = login_resp.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]

    submit_resp = client.get(
        "/api/auth/dev-stub/submit",
        params={
            "email": email,
            "state": state,
            "redirect_uri": "http://testserver/api/auth/callback",
        },
        follow_redirects=False,
    )
    assert submit_resp.status_code == 302, submit_resp.text
    callback_location = submit_resp.headers["location"]

    callback_resp = client.get(callback_location, follow_redirects=False)
    assert callback_resp.status_code == 302, callback_resp.text


def logout(client: TestClient) -> None:
    """Used when a test needs to switch identities mid-flight."""
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204, resp.text
    client.cookies.clear()
