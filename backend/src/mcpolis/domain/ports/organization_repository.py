from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from mcpolis.domain.model.subscription import Subscription


def _now_utc() -> datetime:
    return datetime.now(UTC)


class Organization(BaseModel):
    """An organization tenant.

    ``id`` is the stable internal identifier used everywhere in the
    storage layer. ``slug`` is the URL-friendly handle for Phase 3 URL
    routing (``/mcp/{slug}``). ``display_name`` is the free-text label
    shown in the UI.
    """

    id: str
    slug: str
    display_name: str
    created_at: datetime
    created_by_email: str | None = None
    subscription: Subscription = Field(default_factory=Subscription)


class Membership(BaseModel):
    """A user's membership in an organization."""

    org_id: str
    email: str
    role: str
    created_at: datetime = Field(default_factory=_now_utc)


class OrganizationRepository(Protocol):
    """Persistence for organizations and memberships.

    Standalone mode returns a single hard-coded ``default`` organization
    and refuses to create additional ones. Cloud mode implements the
    full multi-org behavior.
    """

    async def get_organization(self, org_id: str) -> Organization | None: ...

    async def get_by_slug(self, slug: str) -> Organization | None: ...

    async def list_organizations(self) -> list[Organization]: ...

    async def create_organization(
        self,
        slug: str,
        display_name: str,
        created_by_email: str | None = None,
    ) -> Organization:
        """Create a new organization. Raises ``ValueError`` if the slug
        is already taken, or if the backend does not support creation
        (standalone mode)."""
        ...

    async def list_memberships(self, org_id: str) -> list[Membership]: ...

    async def get_memberships_for_email(self, email: str) -> list[Membership]: ...

    async def add_membership(
        self, org_id: str, email: str, role: str
    ) -> Membership: ...

    async def remove_membership(self, org_id: str, email: str) -> None: ...

    async def delete_organization(self, org_id: str) -> None:
        """Delete an organization and all its memberships."""
        ...

    async def update_subscription(
        self, org_id: str, subscription: Subscription,
    ) -> None:
        """Replace the org's subscription. Used by the superadmin
        plan-flip endpoint (the temporary stand-in for billing)."""
        ...

    async def ensure_default_org(self) -> Organization:
        """Create the ``default`` organization if it does not already
        exist. Used at startup in both modes to guarantee there is at
        least one org to scope queries against."""
        ...

