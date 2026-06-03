"""Port for the dashboard's browser-login OAuth flow.

This is a separate surface from the MCP gateway's OAuth provider
(``adapters/auth/mcp_gateway_oauth_provider.py``). The gateway provider
issues bearer tokens to MCP clients; this port models the cookie-issuing
browser flow for the dashboard SPA.

Production wires Google. Local dev / e2e wires a stub adapter that
issues real session cookies signed with the same key, so every test
exercises the same cookie-verify and role-check code as production.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class CompletedLogin(BaseModel):
    """Outcome of a successful provider round-trip.

    Carries only what the dashboard cookie path needs: an email. ``sub``
    and ``raw_id_token`` are kept around for audit / debugging by
    providers that have them (Google) and left empty by ones that don't
    (dev stub).
    """

    email: str
    sub: str | None = None
    raw_id_token: str | None = None


class DashboardOAuthProvider(Protocol):
    """Pluggable adapter for the ``/api/auth/login`` ↔ ``/api/auth/callback`` round-trip.

    Implementations are stateless w.r.t. the ``state`` token — the
    dashboard route module owns the pending-state dict and hands the
    state in/out. The provider is responsible only for the
    user-facing redirect and the code → identity exchange.
    """

    name: str

    async def start_login(
        self,
        *,
        state: str,
        redirect_uri: str,
        join: str | None,
    ) -> str:
        """Return the URL to redirect the browser to.

        ``join`` is the org slug from a join link, when the user
        clicked one. Providers that need it (the dev stub may pre-
        select an org context) can use it; Google ignores it.
        """
        ...

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> CompletedLogin:
        """Exchange the callback parameters for an authenticated identity.

        Raises ``ValueError`` on any provider-side failure
        (token-exchange HTTP error, malformed ID token, etc.) — the
        route translates that to a 400.
        """
        ...
