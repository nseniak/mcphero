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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from pydantic import BaseModel

# Org deletion orchestrates a purge across every org-scoped repository.
# The legacy abstract bases (``ConnectionStore`` / ``UpstreamConfigStore``
# / ``AuditRepository``) live in the adapters layer; importing them here
# mirrors the existing precedent in ``org_runtime`` (same service layer)
# and lets the app wire ``StorageBundle`` fields in without casts.
from mcpolis.adapters.repositories.audit_repository import (
    AuditRepository as LegacyAuditRepository,
)
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.domain.model.email_address import find_address
from mcpolis.domain.model.reserved_slugs import RESERVED_ORG_SLUGS
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.domain.ports import (
    ConfigRepository,
    Membership,
    Organization,
    OrganizationRepository,
    ServiceTokenRepository,
)
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.tool_catalog_repository import ToolCatalogRepository
from mcpolis.domain.services.cancel_shield import runs_to_completion
from mcpolis.domain.services.sandbox_service import (
    SandboxProviderName,
    SandboxService,
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


class Invitation(BaseModel):
    """An org's invitation the invited person has not accepted yet."""

    org: Organization
    role: str


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
        service_token_repo: ServiceTokenRepository | None = None,
        *,
        connection_repo: ConnectionStore | None = None,
        upstream_config_repo: UpstreamConfigStore | None = None,
        tool_catalog_repo: ToolCatalogRepository | None = None,
        sandbox_persistence_repo: SandboxPersistenceRepository | None = None,
        template_var_repo: TemplateVarRepository | None = None,
        sandbox_file_repo: SandboxFileRepository | None = None,
        audit_repo: LegacyAuditRepository | None = None,
        sandbox_services: Mapping[SandboxProviderName, SandboxService] | None = None,
    ) -> None:
        self._org_repo = org_repo
        self._config_repo = config_repo
        self._service_token_repo = service_token_repo
        # Additional org-scoped repos for the deletion cascade. Optional
        # so the many call sites that only exercise create/membership
        # logic stay terse; production (``create_app``) wires every one,
        # matching the org-scoped fields on ``StorageBundle``. A repo left
        # ``None`` is simply skipped during the purge.
        self._connection_repo = connection_repo
        self._upstream_config_repo = upstream_config_repo
        self._tool_catalog_repo = tool_catalog_repo
        self._sandbox_persistence_repo = sandbox_persistence_repo
        self._template_var_repo = template_var_repo
        self._sandbox_file_repo = sandbox_file_repo
        self._audit_repo = audit_repo
        # Provider services used to kill the org's persisted sandboxes
        # before their refs are purged (see ``_release_sandboxes``).
        self._sandbox_services = sandbox_services
        # Late-bound by ``create_app`` (after the slug cache exists) to
        # stop the in-memory runtime + invalidate the slug cache on
        # deletion. Kept as a callback so the domain layer stays
        # decoupled from ``OrgRuntimeManager``.
        self._runtime_teardown: Callable[[str], Awaitable[None]] | None = None

    def set_runtime_teardown(
        self, teardown: Callable[[str], Awaitable[None]],
    ) -> None:
        """Register the in-memory teardown hook run before the persistence
        purge in ``delete_organization`` (mirrors the gateway provider's
        ``set_org_service`` late-binding pattern in ``create_app``)."""
        self._runtime_teardown = teardown

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
        """Return the orgs the user is a member of: one per membership
        row, i.e. per invitation they accepted (or org they created).
        Same rule in both modes; a pending invitation is not listed (see
        ``list_invitations``)."""
        memberships = await self._org_repo.get_memberships_for_email(email)
        orgs: list[Organization] = []
        for m in memberships:
            org = await self._org_repo.get_organization(m.org_id)
            if org is not None:
                orgs.append(org)
        return orgs

    async def _has_membership_row(self, org_id: str, email: str) -> bool:
        memberships = await self._org_repo.get_memberships_for_email(email)
        return any(m.org_id == org_id for m in memberships)

    async def get_user_role(self, org_id: str, email: str) -> str | None:
        """Return the member's role name in this org, or ``None`` for
        anyone who is not a member (a pending invitation included)."""
        config = await self._config_repo.load(org_id)
        key = find_address(config.users, email)
        if key is None or not await self._has_membership_row(org_id, email):
            return None
        return config.users[key].role

    async def is_admin(self, org_id: str, email: str) -> bool:
        """Return True if the member holds an admin-flagged role in this
        org. A pending admin invitation grants nothing.

        Routes through ``RoleDefinition.is_admin`` — independent of the
        role's *name*, so any role flagged ``is_admin=True`` grants
        admin (mirrors :meth:`PolicyEngine.is_admin`). Use this for
        admin gates anywhere outside the policy_engine itself; do not
        compare role names against the literal string ``"admin"``.
        """
        config = await self._config_repo.load(org_id)
        if not resolve_settings(config, email).is_admin:
            return False
        return await self._has_membership_row(org_id, email)

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
        """Whether ``email`` accepted an invitation to (or created) the
        org and is still on its team."""
        return (await self.get_user_role(org_id, email)) is not None

    async def resolve_slug(self, slug: str) -> Organization:
        """Look up an org by slug or raise ``OrgNotFoundError``."""
        org = await self._org_repo.get_by_slug(slug)
        if org is None:
            raise OrgNotFoundError(slug)
        return org

    # --- Memberships ---

    async def list_invitations(self, email: str) -> list[Invitation]:
        """The invitations ``email`` has not accepted yet: every org whose
        users include the address (letter case ignored) while it has no
        membership row there. Oldest org first.

        Invitations are never accepted on the person's behalf. Each one
        waits for its own explicit Join (see
        ``UserAdminService.accept_invitation``); until then the org's
        admins have no power over the person and the person has no
        access to the org.

        The dashboard asks on every page load (``/api/auth/me``):
        ``find_user`` answers without reading every org's config.
        """
        entries = await self._config_repo.find_user(email)
        if not entries:
            return []
        joined = {
            m.org_id
            for m in await self._org_repo.get_memberships_for_email(email)
        }
        invitations: list[Invitation] = []
        for entry in entries:
            if entry.org_id in joined:
                continue
            org = await self._org_repo.get_organization(entry.org_id)
            if org is not None:  # None: the config of a deleted org
                invitations.append(Invitation(org=org, role=entry.user.role))
        return sorted(invitations, key=lambda i: i.org.created_at)

    async def invitation_to(self, slug: str, email: str) -> Invitation | None:
        """``email``'s pending invitation to the org at ``slug``, if any
        (letter case ignored)."""
        org = await self._org_repo.get_by_slug(slug)
        if org is None or await self._has_membership_row(org.id, email):
            return None
        config = await self._config_repo.load(org.id)
        key = find_address(config.users, email)
        if key is None:
            return None
        return Invitation(org=org, role=config.users[key].role)

    async def add_founding_member(
        self, org_id: str, email: str, role: str,
    ) -> Membership:
        """Save the membership of someone who sets the org up rather than
        being invited into it (the first person to sign in to a fresh
        standalone install), like ``create_organization`` does for an
        org's creator."""
        return await self._org_repo.add_membership(org_id, email, role)

    async def list_members(self, org_id: str) -> list[Membership]:
        return await self._org_repo.list_memberships(org_id)

    async def _purge(
        self,
        org_id: str,
        collection: str,
        op: Callable[[], Awaitable[Any]] | None,
    ) -> int:
        """Run one collection's per-org delete, best-effort.

        ``op`` is ``None`` when the repo wasn't wired (skip). A failure
        logs a warning and returns 0 rather than aborting — org deletion
        is terminal, so one collection failing must not strand the rest.
        Returns the row count when the delete reports one, else 0.
        """
        if op is None:
            return 0
        try:
            result = await op()
        except Exception:
            logger.warning(
                "org.delete.purge_failed",
                org_id=org_id,
                collection=collection,
                exc_info=True,
            )
            return 0
        return result if isinstance(result, int) else 0

    @runs_to_completion
    async def delete_organization(self, org_id: str) -> None:
        """Delete an organization and PURGE every collection scoped to it.

        Without this, a deleted tenant's encrypted OAuth tokens, config,
        template-var secrets, sandbox files, and tool catalog would
        linger in storage forever — a privacy leak.

        Order is deliberate:

        1. ``org_repo.delete_organization`` **first**. In standalone mode
           the file repo raises here, so a stray ``DELETE /api/orgs/default``
           is rejected before any runtime teardown or purge runs (it can't
           tear down the live single-tenant runtime or wipe file data).
           In cloud mode this removes the org doc + memberships.
        2. Purge the org's service tokens, at once (see below).
        3. Tear down the in-memory runtime (stop live upstream clients,
           invalidate the slug cache) so nothing writes a row back
           mid-purge.
        4. Purge each other org-scoped repo, best-effort.
        5. Kill every sandbox the org still has on the provider, then
           purge the sandbox refs. A ref whose kill or volume destroy
           failed is kept, listing only that, for the boot reconcile to
           retry: it is the last thing that names such a sandbox (see
           ``_release_sandboxes``).

        Service tokens matter specially: they carry their org in the
        credential and bypass membership gating, so a survivor would keep
        working through the gateway's org-pin path and be unrevocable —
        the org's dashboard no longer exists. So they go right after the
        org itself, before the steps that can take long (closing the
        runtime's sessions, an E2B kill per sandbox): a deletion stopped
        in between, by a crash or a deploy's cut, leaves no live token.
        Nothing writes a token back: using one only updates its
        ``last_used_at``, never re-creates it. The sandboxes go last for
        the same reason: an E2B call per sandbox can take long, and a
        deletion a deploy's shutdown cuts there (once its job drain is
        over) used to leave the org's config, sign-ins and Variables
        behind, with no retry: the org was gone.

        Runs to its end once started (``runs_to_completion``), whatever
        cancels its caller: cut after step 1, the org would be gone, the
        deletion impossible to retry ("not found") and the rest of its
        data left behind.
        """
        await self._org_repo.delete_organization(org_id)

        st = self._service_token_repo
        purged_service_tokens = await self._purge(
            org_id, "service_tokens",
            (lambda: st.delete_for_org(org_id)) if st else None,
        )

        if self._runtime_teardown is not None:
            try:
                await self._runtime_teardown(org_id)
            except Exception:
                logger.warning(
                    "org.delete.runtime_teardown_failed",
                    org_id=org_id,
                    exc_info=True,
                )

        cr = self._connection_repo
        uc = self._upstream_config_repo
        tc = self._tool_catalog_repo
        sp = self._sandbox_persistence_repo
        tv = self._template_var_repo
        sf = self._sandbox_file_repo
        au = self._audit_repo
        counts = {
            "service_tokens": purged_service_tokens,
            "connections": await self._purge(
                org_id, "connections",
                (lambda: cr.delete_all_for_org(org_id)) if cr else None,
            ),
            "config": await self._purge(
                org_id, "config",
                lambda: self._config_repo.delete_for_org(org_id),
            ),
            "upstreams": await self._purge(
                org_id, "upstreams",
                (lambda: uc.delete_all_for_org(org_id)) if uc else None,
            ),
            "tool_catalog": await self._purge(
                org_id, "tool_catalog",
                (lambda: tc.delete_all_for_org(org_id)) if tc else None,
            ),
            "template_vars": await self._purge(
                org_id, "template_vars",
                (lambda: tv.delete_all_for_org(org_id)) if tv else None,
            ),
            "sandbox_files": await self._purge(
                org_id, "sandbox_files",
                (lambda: sf.delete_all_for_org(org_id)) if sf else None,
            ),
            "audit": await self._purge(
                org_id, "audit",
                (lambda: au.delete_for_org(org_id)) if au else None,
            ),
        }

        released, kept_sandbox_refs = await self._release_sandboxes(org_id)
        counts["sandbox_refs"] = await self._purge(
            org_id, "sandbox_refs",
            (
                lambda: self._purge_sandbox_refs(org_id, kept_sandbox_refs)
            ) if sp else None,
        )
        logger.info(
            "org.deleted",
            org_id=org_id,
            sandbox_refs_released=released,
            sandbox_refs_kept=len(kept_sandbox_refs),
            **{f"purged_{name}": n for name, n in counts.items()},
        )

    async def _release_sandboxes(self, org_id: str) -> tuple[int, set[str]]:
        """Kill each persisted sandbox of the org and destroy its storage.

        The runtime teardown only reaches sandboxes with a live session.
        A paused one (DEFERRED_ATTACH, or preserved after E2B paused it)
        is known only to its persisted ref, so purging the ref alone
        would leave it on the provider with nothing pointing at it.
        Same two calls as an upstream's Stop + Delete, fanned out to
        every provider (each is a no-op when it has nothing). Best-effort:
        a failure is logged and the deletion goes on.

        Returns the number of refs processed, and the upstreams whose ref
        a provider kept to finish later (a kill or a destroy that failed,
        see ``SandboxService.on_upstream_removed``): the purge must leave
        those for the boot reconcile, the only thing that still retries
        them. Purged, a sandbox made before the instance id became one
        value per database stayed on the provider for good.
        """
        sp = self._sandbox_persistence_repo
        if sp is None or not self._sandbox_services:
            return 0, set()
        try:
            refs = await sp.list_for_org(org_id=org_id)
        except Exception:
            logger.warning(
                "org.delete.sandbox_list_failed", org_id=org_id, exc_info=True,
            )
            return 0, set()
        kept: set[str] = set()
        for ref in refs:
            for provider, service in self._sandbox_services.items():
                for step, op in (
                    ("kill", service.kill_persisted_session),
                    ("remove", service.on_upstream_removed),
                ):
                    try:
                        ref_kept = await op(
                            org_id=org_id, upstream_id=ref.upstream_id,
                        )
                    except Exception:
                        logger.warning(
                            "org.delete.sandbox_release_failed",
                            org_id=org_id,
                            upstream_id=ref.upstream_id,
                            provider=provider,
                            step=step,
                            exc_info=True,
                        )
                        continue
                    if ref_kept:  # only ``on_upstream_removed`` says so
                        kept.add(ref.upstream_id)
        return len(refs), kept

    async def _purge_sandbox_refs(self, org_id: str, kept: set[str]) -> int:
        """Delete the org's sandbox refs, except those of the upstreams in
        ``kept`` (see ``_release_sandboxes``). Returns how many went."""
        sp = self._sandbox_persistence_repo
        assert sp is not None  # the caller checks
        if not kept:
            return await sp.delete_all_for_org(org_id=org_id)
        deleted = 0
        for ref in await sp.list_for_org(org_id=org_id):
            if ref.upstream_id not in kept:
                await sp.delete(org_id=org_id, upstream_id=ref.upstream_id)
                deleted += 1
        return deleted
