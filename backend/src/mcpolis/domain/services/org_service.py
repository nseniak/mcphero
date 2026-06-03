"""Organization management use cases.

The domain service that sits between the REST layer and the repository
layer for org/invite/membership operations. All cross-repo logic lives
here — routes are thin shells that call into this service.

The service is mode-agnostic: in standalone mode, the underlying
``FileOrganizationRepository`` only knows about the ``default`` org and
raises on ``create_organization``; in cloud mode, ``MongoOrganizationRepository``
implements the full behavior. Callers that need mode-specific behavior
(e.g. hiding the org UI) check ``Settings.mode`` directly — this service
never branches on it.
"""
from __future__ import annotations

import re

import structlog

from mcpolis.domain.model.reserved_slugs import RESERVED_ORG_SLUGS
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.domain.ports import (
    DEFAULT_ORG_ID,
    ConfigRepository,
    Membership,
    Organization,
    OrganizationRepository,
)
from mcpolis.domain.services.settings_resolver import resolve_settings

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Slugs are URL identifiers + the leading segment of cloud multi-org
# tool names: ``{slug}__{upstream}__{tool}``. The MCP / Anthropic Tool
# Use API caps tool names at 64 chars, so the slug ceiling has to leave
# room for the worst-case ``__upstream__tool`` tail. We give each of
# upstream and tool 20 chars (matches the longest existing upstream
# id + tool name we ship today, with headroom) plus the two ``__``
# separators: 64 - 2 - 20 - 2 - 20 = 20. Round to 20.
MAX_SLUG_LENGTH = 20

# Lowercase, alphanumeric + hyphens, no leading/trailing hyphens,
# 3..MAX_SLUG_LENGTH characters. Built from the ceiling so the two
# stay in sync.
_SLUG_PATTERN = re.compile(
    rf"^[a-z0-9][a-z0-9-]{{1,{MAX_SLUG_LENGTH - 2}}}[a-z0-9]$"
)

# Aliased under the historic name for in-module references. The
# authoritative set lives in ``mcpolis.domain.model.reserved_slugs``;
# the middleware imports its own (narrower) set from the same module,
# so the two views are co-located and can't drift unnoticed.
_RESERVED_SLUGS = RESERVED_ORG_SLUGS


class SlugValidationError(ValueError):
    """Raised when a slug fails format or reservation checks."""


class OrgNotFoundError(LookupError):
    """Raised when a slug or org_id lookup returns nothing."""


class NotAMemberError(PermissionError):
    """Raised when the caller is not a member of the target org."""


def validate_slug(slug: str) -> None:
    """Check the slug is well-formed and not reserved. Raises on bad slug."""
    if not _SLUG_PATTERN.match(slug):
        raise SlugValidationError(
            f"Short name must be 3-{MAX_SLUG_LENGTH} characters, lowercase "
            f"letters, numbers, or hyphens, and cannot start or end with a "
            f"hyphen."
        )
    if slug in _RESERVED_SLUGS:
        raise SlugValidationError(f"'{slug}' is a reserved name.")


def suggest_slug(display_name: str) -> str:
    """Derive a URL-friendly slug from a display name.

    The frontend auto-fills the slug field with this when the user types
    the display name; the field stays editable.
    """
    slug = display_name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        slug = "org"
    return slug[:MAX_SLUG_LENGTH].rstrip("-")


class OrgService:
    """Use-case layer for organization management.

    Consumes the ``OrganizationRepository`` and ``ConfigRepository``
    ports. No direct storage access, no Mongo awareness, no mode
    checks. The app wires this once at startup and passes it to the
    org routes.
    """

    def __init__(
        self,
        org_repo: OrganizationRepository,
        config_repo: ConfigRepository,
    ) -> None:
        self._org_repo = org_repo
        self._config_repo = config_repo

    # --- Creation ---

    async def create_organization(
        self,
        *,
        slug: str,
        display_name: str,
        creator_email: str,
    ) -> Organization:
        """Create an org, seed default roles, and add the creator as admin.

        Atomicity: the individual steps are not wrapped in a transaction
        — if ``set_user`` fails after ``create_organization`` succeeds,
        the org will exist without a creator membership. We accept this
        for now; Phase 4 can introduce a cleanup job or Mongo
        transaction if it becomes an issue in practice.
        """
        validate_slug(slug)
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("display_name must not be empty")

        org = await self._org_repo.create_organization(
            slug=slug,
            display_name=display_name,
            created_by_email=creator_email,
        )
        # Seed default roles for the new org (admin + default).
        await self._config_repo.ensure_defaults(org.id)
        admin_role = await self.get_admin_role_name(org.id)
        # Make the creator an admin user in the org's config (so the
        # existing policy engine / dashboard APIs recognize them).
        await self._config_repo.set_user(
            org.id, creator_email, UserDefinition(role=admin_role)
        )
        # Mirror that in the memberships collection (cloud mode only).
        # In standalone mode this is a no-op stub, which is fine.
        await self._org_repo.add_membership(org.id, creator_email, admin_role)
        logger.info(
            "org.created",
            org_slug=slug,
            org_id=org.id,
            creator_email=creator_email,
        )
        return org

    # --- Reads ---

    async def list_user_orgs(self, email: str) -> list[Organization]:
        """Return orgs the user belongs to.

        In cloud mode this queries the memberships collection. In
        standalone mode the file repo returns ``[]`` from
        ``get_memberships_for_email`` — the service falls back to
        checking the default-org config, which is the single source of
        truth for standalone users.
        """
        memberships = await self._org_repo.get_memberships_for_email(email)
        if memberships:
            orgs: list[Organization] = []
            for m in memberships:
                org = await self._org_repo.get_organization(m.org_id)
                if org is not None:
                    orgs.append(org)
            return orgs
        # Fallback for standalone mode: the user either exists in the
        # default org's config or they don't. The file repo never
        # records memberships.
        default_config = await self._config_repo.load(DEFAULT_ORG_ID)
        if email in default_config.users:
            default_org = await self._org_repo.get_organization(DEFAULT_ORG_ID)
            if default_org is not None:
                return [default_org]
        return []

    async def get_user_role(self, org_id: str, email: str) -> str | None:
        """Return the user's role name in this org, or ``None``."""
        config = await self._config_repo.load(org_id)
        user = config.users.get(email)
        if user is None:
            return None
        return user.role

    async def is_admin(self, org_id: str, email: str) -> bool:
        """Return True if the user holds an admin-flagged role in this org.

        Routes through ``RoleDefinition.is_admin`` — independent of the
        role's *name*, so any role flagged ``is_admin=True`` grants
        admin (mirrors :meth:`PolicyEngine.is_admin`). Use this for
        admin gates anywhere outside the policy_engine itself; do not
        compare role names against the literal string ``"admin"``.
        """
        config = await self._config_repo.load(org_id)
        return resolve_settings(config, email).is_admin

    async def get_admin_role_name(self, org_id: str) -> str:
        """Return the seed-time admin role name for this org.

        Used by code that creates the creator membership / OrgSummary
        rows for a newly-minted org. Picks the lexicographically-first
        ``is_admin=True`` role for determinism. Raises ``ValueError``
        if the config has no admin role at all (a misconfiguration:
        ``ensure_defaults`` always seeds one).
        """
        config = await self._config_repo.load(org_id)
        names = sorted(
            name for name, role_def in config.roles.items()
            if role_def.is_admin
        )
        if not names:
            raise ValueError(f"org {org_id} has no admin role")
        return names[0]

    async def is_member(self, org_id: str, email: str) -> bool:
        return (await self.get_user_role(org_id, email)) is not None

    async def resolve_slug(self, slug: str) -> Organization:
        """Look up an org by slug or raise ``OrgNotFoundError``."""
        org = await self._org_repo.get_by_slug(slug)
        if org is None:
            raise OrgNotFoundError(slug)
        return org

    # --- Memberships ---

    async def ensure_memberships_for_user(self, email: str) -> None:
        """Create membership rows for every org the user appears in
        via ``config.users`` but doesn't yet have a membership row.

        Called during the login callback so that users added via the
        Team page's "Add member" (which only writes ``config.users``)
        are marked as active (have a membership row) on their first
        sign-in. This is what lets the Team page distinguish
        "active" from "pending".
        """
        existing = await self._org_repo.get_memberships_for_email(email)
        existing_org_ids = {m.org_id for m in existing}
        # Scan all orgs. In cloud mode the number of orgs is bounded
        # (tens, not millions), so a full scan is fine.
        all_orgs = await self._org_repo.list_organizations()
        for org in all_orgs:
            if org.id in existing_org_ids:
                continue
            config = await self._config_repo.load(org.id)
            user = config.users.get(email)
            if user is not None:
                await self._org_repo.add_membership(
                    org.id, email, user.role,
                )
                logger.info(
                    "org.membership.created_on_first_sign_in",
                    member_email=email,
                    org_id=org.id,
                    org_slug=org.slug,
                    role=user.role,
                )

    async def list_members(self, org_id: str) -> list[Membership]:
        return await self._org_repo.list_memberships(org_id)

    async def delete_organization(self, org_id: str) -> None:
        """Delete an organization and all its memberships."""
        await self._org_repo.delete_organization(org_id)
        logger.info(
            "org.deleted",
            org_id=org_id,
        )
