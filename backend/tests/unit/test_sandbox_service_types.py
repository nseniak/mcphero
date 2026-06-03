"""Type-level checks for the SandboxService boundary.

The parameterized contract suite (``tests/sandbox_service_contract/``)
exercises real backends as they land. These tests cover the value
types directly — invariants enforced by pydantic, equality semantics,
and forward-compat fields on enums — so they run today without any
backend wired in.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.services.sandbox_service import (
    ProviderExitInfo,
    ResourcesUnsupported,
    SandboxCapabilities,
    SandboxResourceCombo,
    SandboxResources,
    SnapshotRef,
)


def make_resources(
    cpu_vcpus: float = 1.0,
    memory_mb: int = 1024,
    disk_gb: int = 0,
    pids_limit: int | None = None,
) -> SandboxResources:
    return SandboxResources(
        cpu_vcpus=cpu_vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        pids_limit=pids_limit,
    )


def make_capabilities(
    provider: str = "own-runner",
    cpus: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0),
    rams: tuple[int, ...] = (256, 512, 1024, 2048),
    disks: tuple[int, ...] = (0, 1, 5),
    supports_pause_resume: bool = False,
    supports_egress_filtering: bool = True,
    supports_persistent_disk: bool = True,
) -> SandboxCapabilities:
    combos = tuple(
        SandboxResourceCombo(cpu_vcpus=c, memory_mb=r, disk_gb=d)
        for c in cpus for r in rams for d in disks
    )
    return SandboxCapabilities(
        provider=provider,  # type: ignore[arg-type]
        allowed_cpu_vcpus=cpus,
        allowed_memory_mb=rams,
        allowed_disk_gb=disks,
        allowed_combinations=combos,
        supports_pause_resume=supports_pause_resume,
        supports_egress_filtering=supports_egress_filtering,
        supports_persistent_disk=supports_persistent_disk,
    )


# ---------- SandboxResources ----------


def test_sandbox_resources_rejects_zero_cpu() -> None:
    with pytest.raises(ValidationError):
        make_resources(cpu_vcpus=0)


def test_sandbox_resources_rejects_negative_cpu() -> None:
    with pytest.raises(ValidationError):
        make_resources(cpu_vcpus=-1)


def test_sandbox_resources_rejects_zero_memory() -> None:
    with pytest.raises(ValidationError):
        make_resources(memory_mb=0)


def test_sandbox_resources_allows_zero_disk() -> None:
    """0 ⇔ provider default / ephemeral — a valid value, not a
    rejection condition."""
    r = make_resources(disk_gb=0)
    assert r.disk_gb == 0


def test_sandbox_resources_rejects_negative_disk() -> None:
    with pytest.raises(ValidationError):
        make_resources(disk_gb=-1)


def test_sandbox_resources_is_frozen() -> None:
    r = make_resources()
    with pytest.raises(ValidationError):
        r.cpu_vcpus = 99.0  # type: ignore[misc]


def test_sandbox_resources_equality() -> None:
    a = make_resources(cpu_vcpus=2.0, memory_mb=2048)
    b = make_resources(cpu_vcpus=2.0, memory_mb=2048)
    assert a == b


# ---------- SandboxCapabilities ----------


def test_capabilities_provider_must_be_known_name() -> None:
    """The Literal narrows ``provider`` at construction time."""
    with pytest.raises(ValidationError):
        SandboxCapabilities(
            provider="not-a-provider",  # type: ignore[arg-type]
            allowed_cpu_vcpus=(1.0,),
            allowed_memory_mb=(1024,),
            allowed_disk_gb=(),
            allowed_combinations=(
                SandboxResourceCombo(
                    cpu_vcpus=1.0, memory_mb=1024, disk_gb=0,
                ),
            ),
            supports_pause_resume=False,
            supports_egress_filtering=False,
            supports_persistent_disk=False,
        )


def test_capabilities_disk_grid_may_be_empty() -> None:
    """E2B's storage isn't user-configurable; an empty disk grid
    is the documented signal to hide the disk control."""
    caps = make_capabilities(disks=())
    assert caps.allowed_disk_gb == ()


def test_capabilities_is_frozen() -> None:
    caps = make_capabilities()
    with pytest.raises(ValidationError):
        caps.supports_pause_resume = True  # type: ignore[misc]


# ---------- SnapshotRef ----------


def test_snapshot_ref_carries_provider() -> None:
    ref = SnapshotRef(provider="e2b", snapshot_id="snap-abc")
    assert ref.provider == "e2b"
    assert ref.snapshot_id == "snap-abc"
    assert ref.metadata == {}


def test_snapshot_ref_provider_must_be_known() -> None:
    with pytest.raises(ValidationError):
        SnapshotRef(provider="bogus", snapshot_id="snap-1")  # type: ignore[arg-type]


def test_snapshot_ref_is_frozen() -> None:
    ref = SnapshotRef(provider="e2b", snapshot_id="snap-1")
    with pytest.raises(ValidationError):
        ref.snapshot_id = "snap-2"  # type: ignore[misc]


# ---------- ResourcesUnsupported ----------


def test_resources_unsupported_carries_field_and_value() -> None:
    err = ResourcesUnsupported("cpu_vcpus", 99.0, allowed=(0.5, 1.0))
    assert err.field == "cpu_vcpus"
    assert err.value == 99.0
    assert err.allowed == (0.5, 1.0)
    assert "cpu_vcpus" in str(err)
    assert "99.0" in str(err)


def test_resources_unsupported_without_allowed_list() -> None:
    err = ResourcesUnsupported("disk_gb", 9999)
    assert err.allowed is None
    assert "9999" in str(err)


def test_resources_unsupported_is_value_error() -> None:
    """Subclasses ``ValueError`` so callers can ``except ValueError``
    in places that don't import the SandboxService boundary."""
    err = ResourcesUnsupported("memory_mb", 32_000)
    assert isinstance(err, ValueError)


# ---------- ProviderExitInfo + ExitReason additions ----------


def test_provider_exit_info_defaults_are_empty() -> None:
    info = ProviderExitInfo()
    assert info.exit_code is None
    assert info.error_class == ""
    assert info.raw_message == ""


def test_provider_exit_info_round_trip() -> None:
    info = ProviderExitInfo(
        exit_code=137, error_class="OutOfMemoryError", raw_message="killed",
    )
    serialised = info.model_dump()
    rebuilt = ProviderExitInfo(**serialised)
    assert rebuilt == info


def test_exit_reason_has_provider_error_value() -> None:
    """The new value bridges coarse-signal backends; the Exit frame
    parser already accepts unknown reasons by mapping to
    ``INTERNAL_ERROR``, so adding a value here is forward-compat."""
    assert ExitReason.PROVIDER_ERROR.value == "provider_error"


def test_exit_reason_has_account_limit_exceeded_value() -> None:
    assert ExitReason.ACCOUNT_LIMIT_EXCEEDED.value == "account_limit_exceeded"
