"""Dashboard ``DashboardOAuthProvider`` adapter that delegates login to Google.

Pulled out of the inline calls that previously lived in
``entrypoints/routes/dashboard_auth.py``. The MCP-gateway OAuth provider
(which also delegates to Google for sub-flow login) is a separate
surface — see ``adapters/auth/mcp_gateway_oauth_provider.py``.
"""
from __future__ import annotations

import base64
import json
from urllib.parse import urlencode

import httpx
import structlog

from mcpolis.domain.ports.dashboard_oauth_provider import CompletedLogin

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class DashboardGoogleProvider:
    """Google sign-in for the dashboard browser flow.

    Implements ``DashboardOAuthProvider`` (structurally — Protocol).
    The dashboard route module owns the pending-state dict and the
    membership / cookie / analytics logic; this class is responsible
    only for building the Google redirect URL and exchanging the
    callback code for an email.
    """

    name = "google"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    async def start_login(
        self,
        *,
        state: str,
        redirect_uri: str,
        join: str | None,
    ) -> str:
        # ``join`` is unused for Google — it's the dashboard's
        # responsibility to remember the join slug across the round-
        # trip via the pending-state dict. Kept in the signature so the
        # port stays uniform with providers that want it (dev stub).
        del join
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> CompletedLogin:
        del state  # state validation is the route's job
        # A transport-level fault talking to Google (timeout, connection
        # refused) is a third-party failure the callback route maps to a
        # clean 400 via the ValueError family — never let a raw httpx
        # exception escape as an unhandled 500 (same class as the gateway
        # provider's handle_google_callback).
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "dashboard.auth.google_token_exchange.failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ValueError("Failed to exchange code with Google") from exc
        if resp.status_code != 200:
            logger.warning(
                "dashboard.auth.google_token_exchange.failed",
                status_code=resp.status_code,
                response_text=resp.text,
            )
            raise ValueError("Failed to exchange code with Google")
        try:
            token_data = resp.json()
        except json.JSONDecodeError as exc:
            # A 200 with a malformed body — map to the ValueError family the
            # callback route maps to a clean 400, rather than letting a raw
            # JSONDecodeError ride out on the route's ``except ValueError``.
            logger.warning(
                "dashboard.auth.google_token_exchange.failed",
                error=str(exc),
            )
            raise ValueError("Failed to exchange code with Google") from exc

        id_token = token_data.get("id_token")
        if not id_token:
            raise ValueError("Google did not return an ID token")

        email = extract_email_from_id_token(id_token)
        if not email:
            raise ValueError("Could not extract email from Google token")

        return CompletedLogin(email=email, raw_id_token=id_token)


def extract_email_from_id_token(id_token: str) -> str | None:
    """Extract email from a Google ID token (JWT) without cryptographic verification.

    We trust this token because we just received it directly from Google's
    token endpoint over HTTPS in exchange for a code we generated.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return None

    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
        return claims.get("email")  # type: ignore[no-any-return]
    except Exception:
        logger.exception("dashboard.auth.google_id_token.decode.failed")
        return None
