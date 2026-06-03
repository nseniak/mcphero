"""Drift guard for the reserved-slug constants.

Every module that needs to know "can this string be an org slug?" or
"is this URL segment an MCP sub-path, not a slug?" imports from
``mcpolis.domain.model.reserved_slugs``. This file asserts that the
constants actually come from there (no shadow copies floating around).

If you add a new reserved name, add it in
``mcpolis.domain.model.reserved_slugs`` — not in the module that uses
it.
"""
from __future__ import annotations

from mcpolis.domain.model.reserved_slugs import (
    RESERVED_MCP_SEGMENTS,
    RESERVED_ORG_SLUGS,
)


def test_mcp_segments_subset_of_org_slugs() -> None:
    """Every MCP sub-path name must also be banned as an org slug,
    otherwise a tenant could create an org with a slug that collides
    with a built-in mount (``/mcp/oauth/...`` vs
    ``/mcp/oauth/authorize``)."""
    missing = RESERVED_MCP_SEGMENTS - RESERVED_ORG_SLUGS
    assert missing == frozenset(), (
        f"MCP segments missing from RESERVED_ORG_SLUGS: {missing}"
    )


def test_default_is_reserved_as_slug_but_not_as_mcp_segment() -> None:
    """``default`` is a runtime slug (the built-in default org) so the
    middleware MUST try to resolve it. But it's also reserved from
    user-chosen org slugs so no tenant can claim it."""
    assert "default" in RESERVED_ORG_SLUGS
    assert "default" not in RESERVED_MCP_SEGMENTS


def test_org_service_uses_shared_constant() -> None:
    """``org_service`` must not keep its own copy of the reserved-slug
    list — drift has caused bugs before. Assert the in-module alias
    points at the shared constant."""
    from mcpolis.domain.services.org_service import _RESERVED_SLUGS

    assert _RESERVED_SLUGS is RESERVED_ORG_SLUGS


def test_middleware_uses_shared_constant() -> None:
    """Same guard for the middleware."""
    from mcpolis.entrypoints.middleware import org_context

    # Cross-module identity check is the whole point of this test —
    # confirm the middleware references the same constant object the
    # domain model exports, not a local copy.
    assert org_context.RESERVED_MCP_SEGMENTS is RESERVED_MCP_SEGMENTS  # pyright: ignore[reportPrivateImportUsage]


def test_validate_slug_rejects_every_reserved_name() -> None:
    """End-to-end: ``validate_slug`` refuses every reserved name."""
    from mcpolis.domain.services.org_service import (
        SlugValidationError,
        validate_slug,
    )
    import pytest

    for name in RESERVED_ORG_SLUGS:
        # Some reserved names don't match the slug pattern at all
        # (e.g. ``.well-known`` starts with a dot). ``validate_slug``
        # rejects those too but with a format error rather than
        # "reserved". Accept either outcome.
        with pytest.raises(SlugValidationError):
            validate_slug(name)
