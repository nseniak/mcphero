from __future__ import annotations

from mcpolis.domain.ports.audit_repository import AuditRepository
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.connection_repository import ConnectionRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredAccessToken,
    StoredRefreshToken,
)
from mcpolis.domain.ports.organization_repository import (
    Membership,
    Organization,
    OrganizationRepository,
)
from mcpolis.domain.ports.rate_limiter import RateLimiter, RateLimitResult
from mcpolis.domain.ports.tool_catalog_repository import (
    ToolCatalogRepository,
    ToolCatalogSnapshot,
)
from mcpolis.domain.ports.upstream_config_repository import UpstreamConfigRepository

DEFAULT_ORG_ID = "default"

# Sentinel naming the always-on admin session that sits alongside
# real per-user sessions. Crosses layer boundaries — session
# bookkeeping, token persistence, tool routing, audit identity — so
# it lives next to ``DEFAULT_ORG_ID`` rather than inside any one
# adapter. Any code comparing a ``user_id`` to this sentinel should
# import the constant, not re-spell the string; a grep for the
# literal is how the admin-safety invariants historically went stale.
ADMIN_USER_ID = "__admin__"

# Sentinel ``current_org_id`` value used by ``OrgContextMiddleware``
# for cloud-mode requests on the fixed ``/mcp`` URL. The gateway
# controller treats this as "fan out across the authenticated user's
# org memberships and aggregate tools with a ``{slug}__`` prefix".
# Real org slugs are validated against ``_SLUG_PATTERN`` (a-z0-9-) so
# the leading double-underscore guarantees no collision.
MULTI_ORG_SENTINEL = "__multi__"

__all__ = [
    "AuditRepository",
    "ConfigRepository",
    "ConnectionRepository",
    "EventStream",
    "Membership",
    "OAuthStateRepository",
    "OAuthStateSnapshot",
    "Organization",
    "OrganizationRepository",
    "RateLimiter",
    "RateLimitResult",
    "StoredAccessToken",
    "StoredRefreshToken",
    "ToolCatalogRepository",
    "ToolCatalogSnapshot",
    "UpstreamConfigRepository",
    "ADMIN_USER_ID",
    "DEFAULT_ORG_ID",
]
