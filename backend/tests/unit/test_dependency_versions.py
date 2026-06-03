from importlib.metadata import version

from packaging.version import Version


def test_starlette_at_least_0_49_1() -> None:
    assert Version(version("starlette")) >= Version("0.49.1"), (
        "CVE-2025-62727: bump starlette past 0.49.1 — "
        "see internal/plans/security-fix-starlette-cve-2025-62727.md"
    )


def test_python_multipart_at_least_0_0_27() -> None:
    assert Version(version("python-multipart")) >= Version("0.0.27"), (
        "CVE-2026-42561: bump python-multipart past 0.0.27 — "
        "see internal/plans/security-fix-python-multipart-cve-2026-42561.md"
    )
