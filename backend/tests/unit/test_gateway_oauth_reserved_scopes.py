"""A human OAuth sign-in must never carry a service-token identity.

Unit-level companions to ``test_gateway_oauth_scope_escalation.py``
(which runs the full attack over HTTP). Two independent defenses:

- the gateway OAuth provider refuses the reserved ``mcpolis:`` scope
  namespace at registration and strips it from everything it issues
  or loads (clients and tokens stored before the fix included);
- the readers (boundary role, org pin) trust only the token TYPE the
  service-token verifier mints, never a scope string.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import mcpolis
from mcpolis.domain.model.service_token import (
    ServiceAccessToken,
    boundary_role_from_access_token,
    is_service_token_auth,
    pinned_org_from_access_token,
    strip_reserved_scopes,
)
from mcpolis.domain.ports.oauth_state_repository import (
    StoredAccessToken,
    StoredRefreshToken,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    _get_boundary_role,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.test_gateway_oauth_consent import finish_google_sign_in
from tests.unit.test_gateway_oauth_scope_escalation import (
    FORGED_SCOPE,
    REDIRECT_URI,
    approve_if_asked,
    make_pre_fix_client,
)
from tests.unit.test_google_oauth import make_provider

FORGED_SCOPES: list[str] = [str(s) for s in FORGED_SCOPE.split()]


def make_forged_client(scope: str | None) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="forged-client",
        redirect_uris=[AnyUrl(REDIRECT_URI)],
        scope=scope,
    )


def make_forged_params() -> AuthorizationParams:
    return AuthorizationParams(
        state="attacker-state",
        scopes=FORGED_SCOPES + ["read"],
        code_challenge="attacker-challenge",
        redirect_uri=AnyUrl(REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
    )


def make_human_user(scopes: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        AccessToken(token="t", client_id="member@example.com", scopes=scopes),
    )


def make_service_access_token(org_id: str = "org-a") -> ServiceAccessToken:
    return ServiceAccessToken(
        token="svct_x", client_id="svc:bot", scopes=[],
        role_name="reader", org_id=org_id,
    )


# ───────────────────── provider: scopes never issued ─────────────────────


@pytest.mark.asyncio
async def test_register_refuses_reserved_scopes() -> None:
    provider = make_provider()
    with pytest.raises(RegistrationError):
        await provider.register_client(make_forged_client(FORGED_SCOPE))
    assert await provider.get_client("forged-client") is None


@pytest.mark.asyncio
async def test_register_refuses_a_single_reserved_scope_among_ordinary_ones() -> None:
    provider = make_provider()
    with pytest.raises(RegistrationError):
        await provider.register_client(make_forged_client("read mcpolis:anything"))


@pytest.mark.asyncio
async def test_register_refuses_reserved_scopes_in_any_case() -> None:
    provider = make_provider()
    with pytest.raises(RegistrationError):
        await provider.register_client(make_forged_client("MCPOLIS:svc Mcpolis:role:admin"))


def test_strip_reserved_scopes_ignores_case() -> None:
    assert strip_reserved_scopes(["MCPOLIS:svc", "Mcpolis:org:x", "read"]) == ["read"]
    assert strip_reserved_scopes(None) == []


@pytest.mark.asyncio
async def test_register_keeps_ordinary_scopes() -> None:
    provider = make_provider()
    await provider.register_client(make_forged_client("read write"))
    client = await provider.get_client("forged-client")
    assert client is not None and client.scope == "read write"


@pytest.mark.asyncio
async def test_sign_in_drops_reserved_scopes_from_code_and_token() -> None:
    """A client stored before the fix (with reserved scopes) must not
    carry them into the sign-in, the authorization code or the token."""
    provider = make_provider()
    await provider.get_client("pre-fix-client")  # loads state first
    stored_client = make_pre_fix_client("pre-fix-client", FORGED_SCOPE + " read")
    provider._clients["pre-fix-client"] = stored_client  # pyright: ignore[reportPrivateUsage]
    client = stored_client.info

    await provider.authorize(client, make_forged_params())
    google_state = next(reversed(provider._pending_auths))  # pyright: ignore[reportPrivateUsage]
    pending = provider._pending_auths[google_state]  # pyright: ignore[reportPrivateUsage]
    assert pending.params.scopes == ["read"]

    after_google = await finish_google_sign_in(provider, google_state, "alice@test.com")
    redirect = await approve_if_asked(provider, after_google)
    code = parse_qs(urlparse(redirect).query)["code"][0]
    stored_code = await provider.load_authorization_code(client, code)
    assert stored_code is not None
    assert stored_code.scopes == ["read"]

    token = await provider.exchange_authorization_code(client, stored_code)
    assert token.scope == "read"


@pytest.mark.asyncio
async def test_load_access_token_drops_reserved_scopes() -> None:
    """Tokens minted before the fix may sit in storage with forged
    scopes; loading them must not hand those scopes downstream."""
    provider = make_provider()
    await provider.mint_test_token("seed@test.com")  # forces state load
    provider._access_tokens["old"] = StoredAccessToken(  # pyright: ignore[reportPrivateUsage]
        token="old", client_id="c", user_email="member@example.com",
        scopes=FORGED_SCOPES + ["read"], expires_at=2**31,
    )
    loaded = await provider.load_access_token("old")
    assert loaded is not None
    assert loaded.scopes == ["read"]
    assert not is_service_token_auth(loaded)


@pytest.mark.asyncio
async def test_refresh_drops_reserved_scopes_from_old_refresh_token() -> None:
    provider = make_provider()
    client = make_forged_client(None)
    await provider.register_client(client)
    old = StoredRefreshToken(
        token="old-refresh", client_id="forged-client",
        user_email="member@example.com",
        scopes=FORGED_SCOPES + ["read"], created_at=time.time(),
    )
    # Stored before the fix: refresh only accepts a token it holds.
    provider._refresh_tokens[old.token] = old  # pyright: ignore[reportPrivateUsage]
    token = await provider.exchange_refresh_token(client, old, [])
    assert token.scope == "read"
    loaded = await provider.load_access_token(token.access_token)
    assert loaded is not None and loaded.scopes == ["read"]


# ─────────────── readers trust the token type, not scopes ───────────────


def test_scopes_alone_never_give_a_boundary_role_or_pinned_org() -> None:
    token = AccessToken(token="t", client_id="member@example.com", scopes=FORGED_SCOPES)
    assert not is_service_token_auth(token)
    assert boundary_role_from_access_token(token) is None
    assert pinned_org_from_access_token(token) is None


def test_service_access_token_gives_role_and_org() -> None:
    token = make_service_access_token()
    assert is_service_token_auth(token)
    assert boundary_role_from_access_token(token) == "reader"
    assert pinned_org_from_access_token(token) == "org-a"


def test_service_access_token_with_empty_org_has_no_pinned_org() -> None:
    """Fail-closed input for the org-pin middleware."""
    assert pinned_org_from_access_token(make_service_access_token(org_id="")) is None


def test_gateway_boundary_role_ignores_forged_scopes_on_human_token() -> None:
    reset = auth_context_var.set(make_human_user(FORGED_SCOPES))
    try:
        assert _get_boundary_role() is None
    finally:
        auth_context_var.reset(reset)


# ─────────────── guard: one minter for ServiceAccessToken ───────────────


def test_only_the_service_token_verifier_mints_service_access_tokens() -> None:
    """The role/org readers trust the TOKEN TYPE. That is only safe while
    nothing but the registry-backed verifier constructs one. A new
    construction site anywhere else in the backend fails this guard."""
    src_root = Path(mcpolis.__file__).parent
    allowed = {src_root / "adapters" / "auth" / "service_token_verifier.py"}
    construction = re.compile(r"(?<!class )\bServiceAccessToken\(")
    sites = sorted(
        str(path.relative_to(src_root))
        for path in src_root.rglob("*.py")
        if path not in allowed and construction.search(path.read_text())
    )
    assert sites == []
    verifier_source = next(iter(allowed)).read_text()
    assert construction.search(verifier_source), "guard regex no longer matches"
