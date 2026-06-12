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


# --- Auth-scope encoding ---
#
# The boundary-resolved (role, org) ride in the SDK-blessed channel —
# ``AccessToken.scopes`` — minted by the gateway's service-token
# verifier and read back by the gateway controller / org-pin
# middleware. The encoding is a domain concern (it defines what a
# service-token credential *means*); the adapter only mints it.

SCOPE_SVC = "mcpolis:svc"
SCOPE_ROLE_PREFIX = "mcpolis:role:"
SCOPE_ORG_PREFIX = "mcpolis:org:"


def is_service_token_auth(scopes: list[str]) -> bool:
    return SCOPE_SVC in scopes


def boundary_role_from_auth_scopes(scopes: list[str]) -> str | None:
    """Role carried by a service-token auth, or None for human auth."""
    if SCOPE_SVC not in scopes:
        return None
    for scope in scopes:
        if scope.startswith(SCOPE_ROLE_PREFIX):
            return scope[len(SCOPE_ROLE_PREFIX):]
    return None


def pinned_org_from_auth_scopes(scopes: list[str]) -> str | None:
    """Org a service token is pinned to, or None for human auth."""
    if SCOPE_SVC not in scopes:
        return None
    for scope in scopes:
        if scope.startswith(SCOPE_ORG_PREFIX):
            return scope[len(SCOPE_ORG_PREFIX):]
    return None
