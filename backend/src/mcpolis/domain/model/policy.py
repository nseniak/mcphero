from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class AuthMode(StrEnum):
    service_account = "service_account"
    admin_oauth = "admin_oauth"
    per_user_oauth = "per_user_oauth"


class UpstreamAuthConfig(BaseModel):
    mode: AuthMode

    # For service_account
    token: str | None = None

    # For OAuth modes (admin_oauth/per_user_oauth)
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = Field(default_factory=list)

    # Runtime-only: set when credentials come from oauth_apps.json
    matched_domain: str | None = Field(default=None, exclude=True)
