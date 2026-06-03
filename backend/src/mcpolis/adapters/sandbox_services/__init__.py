"""Concrete ``SandboxService`` backends.

- :class:`LocalSubprocessSandboxService` — no isolation; the legacy
  ``stdio_client`` path used in dev when ``MCPOLIS_SANDBOX_PROVIDER``
  is unset.
- :class:`E2BSandboxService` — hosted; lives under
  :mod:`mcpolis.adapters.sandbox_e2b`.

The own-runner backend was deleted in plan
``serene-beaming-tulip.md`` §Phase 5 once prod settled on E2B.
"""
from __future__ import annotations

from mcpolis.adapters.sandbox_services.local_subprocess import (
    LocalSubprocessSandboxService,
)

__all__ = [
    "LocalSubprocessSandboxService",
]
