"""Contract: capabilities + resource validation.

Every backend must declare its capability grid and reject off-grid
resource requests with a structured ``ResourcesUnsupported`` error.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.services.sandbox_service import (
    ResourcesUnsupported,
    SandboxCapabilities,
    SandboxResources,
    SandboxService,
)

from tests.unit.sandbox_service_contract.backends import iter_backends


@pytest.mark.parametrize("service", iter_backends())
def test_capabilities_returns_non_empty_grid(service: SandboxService) -> None:
    caps = service.capabilities()
    assert isinstance(caps, SandboxCapabilities)
    assert caps.provider == service.name
    assert len(caps.allowed_cpu_vcpus) >= 1
    assert len(caps.allowed_memory_mb) >= 1
    # ``allowed_disk_gb`` may be empty (E2B fixes storage at template
    # build time), but the rest of the grid must be populated.


@pytest.mark.parametrize("service", iter_backends())
def test_validate_resources_accepts_first_grid_combo(
    service: SandboxService,
) -> None:
    """The lowest-resource combination on each dimension MUST always
    validate — that's the default a freshly-created upstream gets,
    so refusing it would block onboarding. Backends with constrained
    pairings (E2B's matrix isn't a full cross-product) cover the
    rest of their grid in their backend-specific suites."""
    caps = service.capabilities()
    resources = SandboxResources(
        cpu_vcpus=caps.allowed_cpu_vcpus[0],
        memory_mb=caps.allowed_memory_mb[0],
        disk_gb=caps.allowed_disk_gb[0] if caps.allowed_disk_gb else 0,
    )
    service.validate_resources(resources)


@pytest.mark.parametrize("service", iter_backends())
def test_validate_resources_rejects_offgrid_cpu(
    service: SandboxService,
) -> None:
    caps = service.capabilities()
    bad_cpu = max(caps.allowed_cpu_vcpus) + 1024
    bad_ram = caps.allowed_memory_mb[0]
    resources = SandboxResources(cpu_vcpus=bad_cpu, memory_mb=bad_ram, disk_gb=0)
    with pytest.raises(ResourcesUnsupported) as exc_info:
        service.validate_resources(resources)
    assert exc_info.value.field == "cpu_vcpus"


@pytest.mark.parametrize("service", iter_backends())
def test_validate_resources_rejects_offgrid_memory(
    service: SandboxService,
) -> None:
    caps = service.capabilities()
    good_cpu = caps.allowed_cpu_vcpus[0]
    bad_ram = max(caps.allowed_memory_mb) + 1024 * 1024
    resources = SandboxResources(
        cpu_vcpus=good_cpu, memory_mb=bad_ram, disk_gb=0,
    )
    with pytest.raises(ResourcesUnsupported) as exc_info:
        service.validate_resources(resources)
    assert exc_info.value.field == "memory_mb"


@pytest.mark.parametrize("service", iter_backends())
def test_validate_resources_rejects_offgrid_disk(
    service: SandboxService,
) -> None:
    caps = service.capabilities()
    if not caps.allowed_disk_gb:
        pytest.skip("provider does not expose user-configurable disk")
    good_cpu = caps.allowed_cpu_vcpus[0]
    good_ram = caps.allowed_memory_mb[0]
    bad_disk = max(caps.allowed_disk_gb) + 1024
    resources = SandboxResources(
        cpu_vcpus=good_cpu, memory_mb=good_ram, disk_gb=bad_disk,
    )
    with pytest.raises(ResourcesUnsupported) as exc_info:
        service.validate_resources(resources)
    assert exc_info.value.field == "disk_gb"
