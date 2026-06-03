"""Backend registry for the parameterized contract suite.

Tests in this directory call :func:`iter_backends` and parameterize
over its return value. Today every entry is a skip-marked placeholder;
each subsequent step in the SandboxService rollout flips one entry to
a real instance and the relevant contract scenarios start running
against that backend with no further wiring.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _pytest.mark.structures import ParameterSet

from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.adapters.sandbox_services import LocalSubprocessSandboxService

from tests.unit.sandbox_e2b_mock import make_mock_e2b_client

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from mcpolis.domain.services.sandbox_service import (
        SandboxProviderName,
        SandboxService,
    )


def _skip(provider: "SandboxProviderName", reason: str) -> ParameterSet:
    """Return a skip-marked parametrize entry for a backend that
    isn't wired up yet. The id is the provider name so pytest's
    output makes it obvious which backend is missing coverage."""
    return pytest.param(None, id=provider, marks=pytest.mark.skip(reason=reason))


def _live(service: "SandboxService") -> ParameterSet:
    return pytest.param(service, id=service.name)


def iter_backends() -> list[ParameterSet]:
    """Backends suitable for *pure* contract scenarios (capabilities,
    validate_resources, map_exit, pause).
    """
    return [
        _live(LocalSubprocessSandboxService()),
        _live(
            E2BSandboxService(
                make_mock_e2b_client(),
                mcpolis_instance="contract-instance",
                on_timeout_seconds=60,
            ),
        ),
    ]


def iter_session_backends() -> list[ParameterSet]:
    """Backends with a reachable transport in the test environment.

    Used by contract scenarios that actually open ``session()``.
    """

    return [
        _live(LocalSubprocessSandboxService()),
        _skip(
            "e2b",
            "session() needs a backend-typed command (npx/uvx) — "
            "covered by test_e2b_sandbox_service.py with the mock SDK",
        ),
    ]


__all__ = ["iter_backends", "iter_session_backends"]
