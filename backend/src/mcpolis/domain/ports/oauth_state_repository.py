from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from mcp.shared.auth import OAuthClientInformationFull


@dataclass
class StoredAccessToken:
    """Gateway access token minted by ``McpGatewayOAuthProvider``.

    Tokens are user-scoped: a token identifies *who* is authenticated,
    not which org the request is for. The org dimension is resolved at
    request time — for cloud ``/mcp`` requests the gateway controller
    fans out across the user's memberships; for ``/admin-mcp/{slug}``
    requests it's resolved from the URL slug. Either way the token
    itself doesn't pin a tenant.
    """

    token: str
    client_id: str
    user_email: str
    scopes: list[str]
    expires_at: int


@dataclass
class StoredRefreshToken:
    """Gateway refresh token minted by ``McpGatewayOAuthProvider``."""

    token: str
    client_id: str
    user_email: str
    scopes: list[str]
    created_at: float
    expires_at: float | None = None


@dataclass
class OAuthStateSnapshot:
    """All persistent gateway OAuth state.

    The gateway provider keeps its working state in memory and round-
    trips it through a repository: ``load`` rehydrates on startup,
    ``save`` is called after every mutating operation. The storage
    backend decides how to persist the snapshot — JSON on disk for
    the file repo, or an encrypted document in Mongo for cloud mode.

    Auth codes and pending auths are short-lived (5 min / one request)
    and stay in-process. They are not part of the snapshot.
    """

    clients: dict[str, OAuthClientInformationFull] = field(
        default_factory=lambda: {}
    )
    access_tokens: dict[str, StoredAccessToken] = field(
        default_factory=lambda: {}
    )
    refresh_tokens: dict[str, StoredRefreshToken] = field(
        default_factory=lambda: {}
    )


class OAuthStateRepository(Protocol):
    """Persistence for gateway (MCP-client-facing) OAuth state.

    The repository is the only code that knows where the state lives.
    ``McpGatewayOAuthProvider`` treats ``load`` / ``save`` as opaque. In
    cloud mode tokens and client secrets inside the snapshot are
    encrypted at rest before hitting Mongo.

    The state is global, not partitioned by org: gateway tokens
    identify a user and the org dimension is resolved per request from
    the URL (admin-mcp) or the user's memberships (multi-org /mcp).
    """

    async def load(self) -> OAuthStateSnapshot: ...

    async def save(self, snapshot: OAuthStateSnapshot) -> None: ...
