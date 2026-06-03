"""Reserved path segments — two distinct but related sets.

These are the names that MCP Hero's routing layer must protect. They
live here so ``org_service`` (which validates user-chosen slugs) and
``org_context`` (which decides whether a URL segment is a slug or a
sub-path) can't drift.

The two sets answer different questions:

- ``RESERVED_ORG_SLUGS`` — "Can a user pick this as their org's slug?"
  No, because it would collide with a top-level HTTP route (``admin``,
  ``api``, ``health``, ...) or with the default org itself
  (``default``).

- ``RESERVED_MCP_SEGMENTS`` — "When the middleware sees ``/mcp/<x>/``
  or ``/admin-mcp/<x>/``, should it try to resolve ``<x>`` as an org
  slug?" Not when ``<x>`` is one of these — those segments serve
  their own mounts (``.well-known`` metadata, ``oauth`` endpoints,
  the ``system`` superadmin, etc.).

``default`` is in the first set (cloud users can't claim it) but NOT
the second (the middleware's slug resolver legitimately maps it to
the default org). Mixing them into one set breaks standalone routing.
"""
from __future__ import annotations


#: Segments that serve their own mount directly under ``/mcp`` or
#: ``/admin-mcp`` — the middleware skips slug resolution for these.
RESERVED_MCP_SEGMENTS: frozenset[str] = frozenset({
    ".well-known",
    "authorize",
    "callback",
    "oauth",
    "register",
    "system",
    "token",
})


#: Names that would shadow a top-level HTTP route or the built-in
#: default org if used as an org slug. ``org_service.validate_slug``
#: rejects these at org-creation time.
#:
#: Contains ``RESERVED_MCP_SEGMENTS`` plus HTTP routing roots that
#: aren't MCP sub-paths (``admin``, ``api``, ``health``, ...) plus
#: ``default`` (which IS a real slug at runtime but must only ever
#: refer to the built-in org, not a user-created one).
RESERVED_ORG_SLUGS: frozenset[str] = RESERVED_MCP_SEGMENTS | frozenset({
    "admin",
    "admin-mcp",
    "api",
    "assets",
    "default",
    "health",
    "login",
    "logout",
    "mcp",
    "signup",
    "static",
    "well-known",  # spelled without the leading dot in validator context
})
