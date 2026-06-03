"""Contract: ``map_exit`` translates provider-native failures.

Coarse-signal backends fall back to ``PROVIDER_ERROR`` + the raw
provider message; rich-signal backends emit specific enum values.
Either way, the caller always receives an ``ExitReason`` and a detail
string suitable for the admin UI's free-text rendering.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.services.sandbox_service import (
    ProviderExitInfo,
    SandboxService,
)

from tests.unit.sandbox_service_contract.backends import iter_backends


@pytest.mark.parametrize("service", iter_backends())
def test_map_exit_returns_known_enum_value(
    service: SandboxService,
) -> None:
    """The first element of the pair must always be a real
    ``ExitReason`` member — never a raw string, never ``None``."""
    info = ProviderExitInfo(
        exit_code=1, error_class="GenericError", raw_message="boom",
    )
    reason, detail = service.map_exit(info)
    assert isinstance(reason, ExitReason)
    # If the backend doesn't have a specific category for this kind
    # of generic failure, ``PROVIDER_ERROR`` + a populated detail
    # string is the documented contract.
    if reason is ExitReason.PROVIDER_ERROR:
        assert detail is not None and detail


@pytest.mark.parametrize("service", iter_backends())
def test_map_exit_handles_empty_input(
    service: SandboxService,
) -> None:
    """Even with no signal, the mapper must produce *some* enum
    value rather than crashing — backends should default to
    ``PROVIDER_ERROR`` or ``INTERNAL_ERROR``."""
    info = ProviderExitInfo()
    reason, _detail = service.map_exit(info)
    assert isinstance(reason, ExitReason)
