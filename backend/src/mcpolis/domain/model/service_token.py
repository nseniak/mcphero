"""Service tokens — non-interactive bearer credentials for the gateway.

A service token lets a headless agent (CI job, scheduled bot, an LLM
agent in a pod) connect to the ``/mcp`` gateway without the interactive
Google OAuth flow. Each token is pinned to exactly one org and one role
at mint time; the identity that flows through policy decisions, audit
entries, and log context is ``svc:<label>`` — never an email, and never
an entry in ``config.users`` (service identities must not appear on the
Team page or count toward seats).

Only the sha256 hash of the raw token is persisted. The raw value is
shown exactly once at mint time. Unsalted sha256 is sound here: the
secret part is 256 bits of CSPRNG entropy, so precomputation attacks
that salting defends against don't apply, and the hash doubles as the
O(1) lookup key on the hot verify path.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel

# Raw-token prefix. Lets the gateway's composite verifier dispatch to
# the registry without touching the OAuth token store, and gives
# secret-scanning tools a greppable shape.
SERVICE_TOKEN_PREFIX = "svct_"

# Identity prefix for the ``user_id`` string that flows through policy,
# audit, and logs. Real org slugs and emails can't collide with it.
SVC_IDENTITY_PREFIX = "svc:"


class ServiceTokenRecord(BaseModel):
    token_hash: str
    org_id: str
    label: str
    role_name: str
    created_by: str
    created_at: datetime
    last_used_at: datetime | None = None


def generate_service_token() -> str:
    return SERVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_service_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def service_identity(label: str) -> str:
    return f"{SVC_IDENTITY_PREFIX}{label}"


def is_service_identity(user_id: str) -> bool:
    return user_id.startswith(SVC_IDENTITY_PREFIX)


# --- Auth-boundary encoding ---
#
# The boundary-resolved (role, org) ride on the AccessToken itself, as
# typed fields of ``ServiceAccessToken``. Only the service-token
# verifier constructs one, so only a registry lookup can confer a
# service identity. They used to ride as scopes (``mcpolis:svc``,
# ``mcpolis:role:<role>``, ``mcpolis:org:<org>``); scopes are client
# input on the OAuth path (dynamic client registration accepts any
# scope string), so a human could request them and be treated as an
# admin service identity pinned to any org.

# Scope namespace the platform reserves, matched case-insensitively.
# The gateway OAuth provider refuses to register it and strips it from
# anything it issues or loads, so a token never carries it. This is a
# second barrier: nothing authorizes on scopes in the first place.
RESERVED_SCOPE_PREFIX = "mcpolis:"


class ServiceAccessToken(AccessToken):
    """AccessToken minted by the service-token verifier, and only there."""

    role_name: str
    org_id: str


def is_reserved_scope(scope: str) -> bool:
    return scope.casefold().startswith(RESERVED_SCOPE_PREFIX)


def strip_reserved_scopes(scopes: list[str] | None) -> list[str]:
    return [s for s in scopes or [] if not is_reserved_scope(s)]


def is_service_token_auth(token: AccessToken) -> bool:
    return isinstance(token, ServiceAccessToken)


def boundary_role_from_access_token(token: AccessToken) -> str | None:
    """Role carried by a service-token auth, or None for human auth."""
    if isinstance(token, ServiceAccessToken):
        return token.role_name
    return None


def pinned_org_from_access_token(token: AccessToken) -> str | None:
    """Org a service token is pinned to, or None for human auth (or an
    empty org, which callers must treat as fail-closed)."""
    if isinstance(token, ServiceAccessToken):
        return token.org_id or None
    return None
