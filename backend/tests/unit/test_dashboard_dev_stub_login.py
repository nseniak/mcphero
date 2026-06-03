"""End-to-end test for the dev-stub dashboard login flow.

Walks the same code paths the production Google flow does:
``/login`` → provider redirect → submit → ``/callback`` → signed
cookie. This is the integration coverage Phase B introduces so the
real cookie-issue + cookie-verify + role-check path is exercised in
tests for the first time (the existing ``test_dashboard_api.py``
suite uses the legacy ``oauth_enabled=False`` header path; Phase C
migrates those tests off it).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings

MCP_JSON = json.dumps({"mcpServers": {}})
CONFIG_JSON = json.dumps({
    "upstreams": {},
    "roles": {
        "admin": {"is_admin": True, "settings": {"mcp_access": {"mcps": {}}}},
        "developer": {"settings": {"mcp_access": {"mcps": {}}}},
    },
    "users": {
        "admin@example.com": {"role": "admin"},
        "dev@example.com": {"role": "developer"},
    },
})


def make_dev_stub_client(tmp_path: Path) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(MCP_JSON)
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        # Real signing key the cookie HMAC is derived from. Any non-
        # empty value works; cloud mode would reject the dev default
        # but standalone mode accepts it.
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


def test_login_redirects_to_dev_stub_picker(tmp_path: Path) -> None:
    client = make_dev_stub_client(tmp_path)
    resp = client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("/api/auth/dev-stub/picker?")
    assert "state=" in location


def test_picker_renders_html_form(tmp_path: Path) -> None:
    client = make_dev_stub_client(tmp_path)
    resp = client.get(
        "/api/auth/dev-stub/picker",
        params={
            "state": "deadbeef",
            "redirect_uri": "http://testserver/api/auth/callback",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<form" in body
    assert 'name="email"' in body
    assert 'value="deadbeef"' in body  # state echoed back as hidden field


def test_picker_pre_fills_first_admin_from_policy(tmp_path: Path) -> None:
    """Phase E: the picker sources its shortlist from the default
    org's policy and pre-fills the first admin so first-time setup is
    one click. Non-admins still appear in the suggestions (the input
    has a datalist) but don't take the default slot."""
    client = make_dev_stub_client(tmp_path)
    resp = client.get(
        "/api/auth/dev-stub/picker",
        params={
            "state": "s",
            "redirect_uri": "http://testserver/api/auth/callback",
        },
    )
    body = resp.text
    # admin@example.com is the only admin in the seeded config and
    # admins always sort ahead of non-admins, so the pre-filled value
    # must be the admin.
    assert 'value="admin@example.com"' in body
    # Non-admin still appears in the datalist for switching mid-session.
    assert "dev@example.com" in body
    # Datalist ordering: admins first.
    admin_pos = body.index("admin@example.com")
    dev_pos = body.index("dev@example.com")
    assert admin_pos < dev_pos


def test_full_login_flow_issues_signed_cookie(tmp_path: Path) -> None:
    """The whole dance: /login → picker → submit → /callback → cookie.

    After this, calling /api/auth/me with the cookie returns the
    authenticated user — proving the cookie went through the
    production verify path, not the legacy header fallback."""
    client = make_dev_stub_client(tmp_path)

    login_resp = client.get("/api/auth/login", follow_redirects=False)
    location = login_resp.headers["location"]
    state = _extract_state(location)

    submit_resp = client.get(
        "/api/auth/dev-stub/submit",
        params={
            "email": "admin@example.com",
            "state": state,
            "redirect_uri": "http://testserver/api/auth/callback",
        },
        follow_redirects=False,
    )
    assert submit_resp.status_code == 302
    callback_location = submit_resp.headers["location"]
    assert callback_location.startswith("/api/auth/callback?")

    callback_resp = client.get(callback_location, follow_redirects=False)
    assert callback_resp.status_code == 302
    cookies_set = callback_resp.headers.get("set-cookie", "")
    assert "mcpolis_session=" in cookies_set
    assert "HttpOnly" in cookies_set

    # The TestClient's cookie jar carries the cookie automatically;
    # /me reads it and runs the production cookie-verify path.
    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 200
    body = me_resp.json()
    assert body["email"] == "admin@example.com"
    assert body["is_admin"] is True


def test_callback_rejects_unknown_state(tmp_path: Path) -> None:
    """State must come from a real /login. A naked /callback with a
    fabricated state can't impersonate anyone."""
    client = make_dev_stub_client(tmp_path)
    resp = client.get(
        "/api/auth/callback",
        params={
            "code": "attacker@example.com",
            "state": "fabricated-state",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_submit_rejects_invalid_email(tmp_path: Path) -> None:
    client = make_dev_stub_client(tmp_path)
    resp = client.get(
        "/api/auth/dev-stub/submit",
        params={
            "email": "not-an-email",
            "state": "anything",
            "redirect_uri": "http://testserver/api/auth/callback",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_logout_clears_cookie(tmp_path: Path) -> None:
    client = make_dev_stub_client(tmp_path)
    _login_as(client, "admin@example.com")

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert "mcpolis_session=" in set_cookie
    # delete_cookie sets an empty value with Max-Age=0 / past expiry
    assert "Max-Age=0" in set_cookie or 'mcpolis_session=""' in set_cookie or "expires=" in set_cookie.lower()


def test_admin_removed_from_org_loses_access_with_existing_cookie(
    tmp_path: Path,
) -> None:
    """The bug class the old header-based tests could not catch:
    a user has a still-signed, still-unexpired cookie, but their
    membership has been revoked since it was issued.

    Before this refactor, ``oauth_enabled=False`` short-circuited
    ``get_current_user`` to read ``x-mcpolis-user`` and
    ``require_admin`` returned the email *without* checking roles —
    so a removed admin's stale cookie would have continued working.
    Now every request runs the cookie-verify + policy-membership
    check, and an admin removed via the Team page gets 403 on the
    next admin-scoped call.
    """
    client = make_dev_stub_client(tmp_path)
    _login_as(client, "admin@example.com")
    victim_cookie = client.cookies.get("mcpolis_session")
    assert victim_cookie is not None

    # Sanity: while admin, the request succeeds.
    resp = client.get("/api/admin/users")
    assert resp.status_code == 200

    # Add a second admin so the eventual delete isn't blocked by the
    # "can't remove the last admin" guard, then re-login as that
    # second admin — we need a different identity to perform the
    # delete (you can't delete the user whose cookie is making the
    # request).
    add = client.post(
        "/api/admin/users",
        json={"email": "second@example.com", "role": "admin"},
    )
    assert add.status_code == 201, add.text

    # Switch to second admin. Clear the jar *without* calling
    # /logout — that would deny-list the saved cookie's jti and
    # short-circuit the membership check we want to exercise. We
    # specifically need the cookie to still verify by HMAC + jti so
    # the failure is forced to come from the policy check.
    client.cookies.clear()
    _login_as(client, "second@example.com")
    remove = client.delete("/api/admin/users/admin@example.com")
    assert remove.status_code == 200, remove.text

    # Replay the original admin's cookie. HMAC still verifies, but
    # the policy-membership check should reject the removed user.
    client.cookies.clear()
    client.cookies.set("mcpolis_session", victim_cookie)
    resp = client.get("/api/admin/users")
    assert resp.status_code == 403, resp.text


def _extract_state(location: str) -> str:
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(location).query)["state"][0]


def _login_as(client: TestClient, email: str) -> None:
    login_resp = client.get("/api/auth/login", follow_redirects=False)
    state = _extract_state(login_resp.headers["location"])
    submit_resp = client.get(
        "/api/auth/dev-stub/submit",
        params={
            "email": email,
            "state": state,
            "redirect_uri": "http://testserver/api/auth/callback",
        },
        follow_redirects=False,
    )
    callback_location = submit_resp.headers["location"]
    client.get(callback_location, follow_redirects=False)
