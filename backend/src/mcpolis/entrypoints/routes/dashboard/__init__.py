"""Per-concern dashboard route files.

The top-level :mod:`mcpolis.entrypoints.routes.dashboard_api` module
re-exports :func:`create_dashboard_api_router`, which composes one
``APIRouter`` per concern from the modules in this package:

- :mod:`._deps` — ``DashboardDeps`` dataclass + module helpers shared
  across concerns (audit logging, policy-change broadcast, OAuth
  ownership scan, upstream readiness resolver, SSE-encode helper).

Route files (one per concern) will be added incrementally as the
``dashboard_api.py`` split lands. Until then, the existing factory in
``dashboard_api.py`` continues to construct every router inline; it
just builds a ``DashboardDeps`` first and passes it through where the
per-concern files will eventually live.
"""
