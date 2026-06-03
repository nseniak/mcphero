"""E2B-backed ``SandboxService``.

Wraps the E2B Python SDK behind the unified
:class:`SandboxService` boundary. Exposed pieces:

- :class:`E2BSandboxService` — the SandboxService impl.
- :class:`E2BClient` — the Protocol over the SDK methods we use,
  injectable for tests.
- :class:`E2BSandboxInfo` — return shape for ``E2BClient.list``.
- :class:`E2BTemplateGrid` — single source of truth for the
  language × (cpu, ram) pairings the operator publishes via
  ``runner/e2b-templates/``. Drives capabilities + template lookup.
- :class:`E2BSDKError` — provider-side exception wrapper.

Real-SDK integration lives in :mod:`mcpolis.adapters.sandbox_e2b.real_client`
and is opt-in: it imports ``e2b`` lazily so the backend boots in
test/dev environments where the SDK isn't installed.
"""
from __future__ import annotations

from mcpolis.adapters.sandbox_e2b.client import (
    E2BAuthError,
    E2BClient,
    E2BNotFoundError,
    E2BProcessHandle,
    E2BQuotaError,
    E2BSandboxHandle,
    E2BSandboxInfo,
    E2BSDKError,
)
from mcpolis.adapters.sandbox_e2b.real_client import (
    RealE2BClient,
    make_real_e2b_client,
)
from mcpolis.adapters.sandbox_e2b.reconciler import E2BSandboxReconciler
from mcpolis.adapters.sandbox_e2b.service import E2BSandboxService
from mcpolis.adapters.sandbox_e2b.template_grid import (
    E2BTemplateGrid,
    template_name_for,
)

__all__ = [
    "E2BAuthError",
    "E2BClient",
    "E2BNotFoundError",
    "E2BProcessHandle",
    "E2BQuotaError",
    "E2BSandboxHandle",
    "E2BSandboxInfo",
    "E2BSandboxReconciler",
    "E2BSandboxService",
    "E2BSDKError",
    "E2BTemplateGrid",
    "RealE2BClient",
    "make_real_e2b_client",
    "template_name_for",
]
