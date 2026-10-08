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


def client_approval_key(
    user_email: str, client_id: str, redirect_identity: str,
) -> str:
    """The key a ``StoredClientApproval`` is stored under."""
    return f"{user_email}\x1f{client_id}\x1f{redirect_identity}"


@dataclass
class StoredClientApproval:
    """A user's remembered consent for one dynamically registered client.

    The gateway delegates identity to Google with a *static* Google
    client id, so without an mcpolis-side consent step any registered
    client could ride a victim's Google login (the MCP spec's "confused
    deputy"). The gateway stops at its own consent page the first time a
    given user authorizes a given client; once approved, the (user,
    client) pair is remembered here so real clients prompt only once.

    Keyed by ``client_approval_key`` — the approval binds to the
    ``scheme://host`` the user saw on the consent page, not to the
    client id alone, so one approval can't be reused to deliver a code
    to a different host the same client also registered.
    """

    user_email: str
    client_id: str
    redirect_identity: str
    approved_at: float

    @property
    def key(self) -> str:
        return client_approval_key(
            self.user_email, self.client_id, self.redirect_identity,
        )


@dataclass
class StoredClient:
    """A client registered through the gateway's open registration.

    ``registered_at`` and ``token_issued`` drive the clean-up of
    abandoned registrations: anyone may register a client anonymously,
    so a registration that never received a token is forgotten after a
    while. A client that did receive one is kept for good, because
    clients like claude.ai reuse their ``client_id`` long after their
    last sign-in.
    """

    info: OAuthClientInformationFull
    registered_at: float
    token_issued: bool

    @classmethod
    def from_before_expiry(
        cls, info: OAuthClientInformationFull,
    ) -> StoredClient:
        """A client stored before registrations could expire.

        Whether it ever received a token is unknown, so it is kept like
        one that did: forgetting a client someone still uses breaks
        their next sign-in."""
        return cls(
            info=info,
            registered_at=float(info.client_id_issued_at or 0),
            token_issued=True,
        )


@dataclass
class OAuthStateSnapshot:
    """All persistent gateway OAuth state, as loaded at startup.

    The gateway provider keeps its working state in memory: ``load``
    fills it on startup, and every change is written back item by item
    through ``OAuthStateRepository.apply``.

    Auth codes, pending sign-ins and pending consents are short-lived
    (minutes) and stay in-process. They are not part of the snapshot.
    """

    clients: dict[str, StoredClient] = field(default_factory=lambda: {})
    access_tokens: dict[str, StoredAccessToken] = field(
        default_factory=lambda: {}
    )
    refresh_tokens: dict[str, StoredRefreshToken] = field(
        default_factory=lambda: {}
    )
    client_approvals: dict[str, StoredClientApproval] = field(
        default_factory=lambda: {}
    )

    def copy(self) -> OAuthStateSnapshot:
        return OAuthStateSnapshot(
            clients=dict(self.clients),
            access_tokens=dict(self.access_tokens),
            refresh_tokens=dict(self.refresh_tokens),
            client_approvals=dict(self.client_approvals),
        )

    def apply(self, changes: OAuthStateChanges) -> None:
        _apply_kind(self.clients, changes.clients)
        _apply_kind(self.access_tokens, changes.access_tokens)
        _apply_kind(self.refresh_tokens, changes.refresh_tokens)
        _apply_kind(self.client_approvals, changes.client_approvals)


def _apply_kind[T](items: dict[str, T], changes: dict[str, T | None]) -> None:
    for key, item in changes.items():
        if item is None:
            items.pop(key, None)
        else:
            items[key] = item


@dataclass
class OAuthStateChanges:
    """Items to write, by kind and key.

    A value stores the item under its key (added or replaced); ``None``
    deletes it. Keys are the ones ``OAuthStateSnapshot`` uses: client
    id, token string, ``StoredClientApproval.key``.
    """

    clients: dict[str, StoredClient | None] = field(
        default_factory=lambda: {}
    )
    access_tokens: dict[str, StoredAccessToken | None] = field(
        default_factory=lambda: {}
    )
    refresh_tokens: dict[str, StoredRefreshToken | None] = field(
        default_factory=lambda: {}
    )
    client_approvals: dict[str, StoredClientApproval | None] = field(
        default_factory=lambda: {}
    )

    def __len__(self) -> int:
        return (
            len(self.clients)
            + len(self.access_tokens)
            + len(self.refresh_tokens)
            + len(self.client_approvals)
        )


class OAuthStateRepository(Protocol):
    """Persistence for gateway (MCP-client-facing) OAuth state.

    The repository is the only code that knows where the state lives.
    Each client, token and approval is stored on its own, so no single
    record grows with the number of registrations or sign-ins. In cloud
    mode every item is encrypted at rest before it reaches Mongo.

    The state is global, not partitioned by org: gateway tokens
    identify a user and the org dimension is resolved per request from
    the URL (admin-mcp) or the user's memberships (multi-org /mcp).
    """

    async def load(self) -> OAuthStateSnapshot:
        """Everything stored, expired items included: the provider owns
        the expiry rules and deletes what has expired."""
        ...

    async def apply(self, changes: OAuthStateChanges) -> None:
        """Write ``changes``. Raises if any of them could not be
        written; some may have been written by then. Writing the same
        changes again is harmless."""
        ...
