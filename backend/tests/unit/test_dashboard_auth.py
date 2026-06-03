"""Tests for dashboard auth: cookie signing/verification."""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_org_slug,
)
from mcpolis.entrypoints.routes.dashboard_auth import (
    build_session_cookie,
    create_dashboard_auth,
    get_signing_key,
    _sign_cookie,
    _verify_cookie,
)


def make_settings(
    session_secret: str = "test-secret",
    superadmin_emails: str = "",
) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        session_secret=session_secret,
        superadmin_emails=superadmin_emails,
    )


def test_sign_and_verify_cookie() -> None:
    settings = make_settings()
    key = get_signing_key(settings)
    payload = {"email": "alice@example.com", "iat": time.time(), "exp": time.time() + 3600}
    cookie = _sign_cookie(payload, key)
    result = _verify_cookie(cookie, key)
    assert result is not None
    assert result["email"] == "alice@example.com"


def test_verify_cookie_rejects_tampered() -> None:
    settings = make_settings()
    key = get_signing_key(settings)
    payload = {"email": "alice@example.com", "iat": time.time(), "exp": time.time() + 3600}
    cookie = _sign_cookie(payload, key)
    # Tamper with the payload
    tampered = "x" + cookie[1:]
    assert _verify_cookie(tampered, key) is None


def test_verify_cookie_rejects_expired() -> None:
    settings = make_settings()
    key = get_signing_key(settings)
    payload = {"email": "alice@example.com", "iat": time.time() - 7200, "exp": time.time() - 3600}
    cookie = _sign_cookie(payload, key)
    assert _verify_cookie(cookie, key) is None


def test_verify_cookie_rejects_wrong_key() -> None:
    key1 = get_signing_key(make_settings("secret-1"))
    key2 = get_signing_key(make_settings("secret-2"))
    payload = {"email": "alice@example.com", "iat": time.time(), "exp": time.time() + 3600}
    cookie = _sign_cookie(payload, key1)
    assert _verify_cookie(cookie, key2) is None


def test_verify_cookie_rejects_garbage() -> None:
    key = get_signing_key(make_settings())
    assert _verify_cookie("not-a-cookie", key) is None
    assert _verify_cookie("", key) is None
    assert _verify_cookie("a.b.c", key) is None


# ── Super-admin bypass in require_admin / get_current_user ───────────
#
# A super-admin browsing another org via the cross-org dashboard has no
# membership row there, so the per-org admin / "still in policy" checks
# would wrongly reject them. These tests drive the dependency closures
# with fake runtimes that return *no* role for the super-admin.


class FakePolicyEngine:
    """Stand-in policy engine: a fixed admin set and role table."""

    def __init__(
        self, admins: set[str], roles: dict[str, list[str]],
    ) -> None:
        self._admins = admins
        self._roles = roles

    def is_admin(self, email: str) -> bool:
        return email in self._admins

    def get_user_roles(self, email: str) -> list[str]:
        return self._roles.get(email, [])


class FakeRuntime:
    def __init__(self, policy_engine: FakePolicyEngine) -> None:
        self.policy_engine = policy_engine


class FakeRuntimeManager:
    """Single-runtime manager — every org_id resolves to the same one."""

    def __init__(self, runtime: FakeRuntime) -> None:
        self._runtime = runtime

    async def get(self, org_id: str) -> FakeRuntime:
        del org_id
        return self._runtime

    def get_cached(self, org_id: str) -> FakeRuntime:
        del org_id
        return self._runtime


def make_dashboard_auth(
    *,
    superadmin_emails: str,
    admins: set[str],
    roles: dict[str, list[str]],
    session_secret: str = "test-session-secret",
):
    """Build a DashboardAuth with fake org runtime + DI'd settings.

    ``policy_store`` / ``org_service`` / ``dashboard_oauth`` aren't
    touched by ``require_admin`` / ``get_current_user``, so plain
    stand-ins suffice (the latter only needs to lack ``register_routes``
    so router wiring is skipped).
    """
    settings = make_settings(
        session_secret=session_secret, superadmin_emails=superadmin_emails,
    )
    runtime = FakeRuntime(FakePolicyEngine(admins, roles))
    runtime_manager = FakeRuntimeManager(runtime)
    return settings, create_dashboard_auth(
        settings,
        runtime_manager,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type] policy_store unused here
        None,  # type: ignore[arg-type] org_service unused here
        object(),  # type: ignore[arg-type] dashboard_oauth (no register_routes)
    )


@pytest.mark.asyncio
async def test_require_admin_allows_superadmin_in_foreign_org() -> None:
    """Super-admin isn't an admin (or even a member) of the target org,
    yet ``require_admin`` lets them through."""
    _settings, auth = make_dashboard_auth(
        superadmin_emails="super@admin.com",
        admins=set(),  # nobody is an org admin here
        roles={},
    )
    tok = current_org_id.set("acme-org-id")
    try:
        result = await auth.require_admin(email="super@admin.com")
    finally:
        current_org_id.reset(tok)
    assert result == "super@admin.com"


@pytest.mark.asyncio
async def test_require_admin_rejects_non_admin_non_superadmin() -> None:
    """A normal non-admin in the target org is still rejected — the
    bypass is gated strictly on the super-admin allowlist."""
    _settings, auth = make_dashboard_auth(
        superadmin_emails="super@admin.com",
        admins={"orgadmin@acme.com"},
        roles={"bob@acme.com": ["developer"]},
    )
    tok = current_org_id.set("acme-org-id")
    try:
        with pytest.raises(HTTPException) as exc:
            await auth.require_admin(email="bob@acme.com")
    finally:
        current_org_id.reset(tok)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_allows_real_org_admin() -> None:
    _settings, auth = make_dashboard_auth(
        superadmin_emails="",
        admins={"orgadmin@acme.com"},
        roles={"orgadmin@acme.com": ["admin"]},
    )
    tok = current_org_id.set("acme-org-id")
    try:
        result = await auth.require_admin(email="orgadmin@acme.com")
    finally:
        current_org_id.reset(tok)
    assert result == "orgadmin@acme.com"


@pytest.mark.asyncio
async def test_get_current_user_keeps_superadmin_without_role() -> None:
    """With an org slug pinned, the "user removed" check would normally
    403 someone holding no role in that org. A super-admin is exempt so
    the cross-org drill-down doesn't bounce them."""
    settings, auth = make_dashboard_auth(
        superadmin_emails="super@admin.com",
        admins=set(),
        roles={},  # super-admin has no role in this org
    )
    cookie = build_session_cookie(
        settings, email="super@admin.com", org_slug="acme",
    )
    id_tok = current_org_id.set("acme-org-id")
    slug_tok = current_org_slug.set("acme")
    try:
        email = await auth.get_current_user(
            request=None, mcpolis_session=cookie,
        )
    finally:
        current_org_id.reset(id_tok)
        current_org_slug.reset(slug_tok)
    assert email == "super@admin.com"


@pytest.mark.asyncio
async def test_get_current_user_rejects_removed_non_superadmin() -> None:
    """The "user removed" guard still fires for a normal user who holds
    no role in the pinned org."""
    settings, auth = make_dashboard_auth(
        superadmin_emails="super@admin.com",
        admins=set(),
        roles={},  # bob has no role here either
    )
    cookie = build_session_cookie(
        settings, email="bob@acme.com", org_slug="acme",
    )
    id_tok = current_org_id.set("acme-org-id")
    slug_tok = current_org_slug.set("acme")
    try:
        with pytest.raises(HTTPException) as exc:
            await auth.get_current_user(
                request=None, mcpolis_session=cookie,
            )
    finally:
        current_org_id.reset(id_tok)
        current_org_slug.reset(slug_tok)
    assert exc.value.status_code == 403
