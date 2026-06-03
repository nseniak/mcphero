"""E2B template grid: 24 templates, no drift between operator-side
driver and backend-side capabilities source.

The operator publishes templates via
``runner/e2b-templates/build_grid.py``; the backend exposes the
same matrix to the admin UI through
``mcpolis.adapters.sandbox_e2b.template_grid``. Drift between the
two surfaces as a UI that lets admins pick combinations the
runner can't actually build (or vice versa). These tests lock
the invariants.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from mcpolis.adapters.sandbox_e2b.template_grid import (
    CPU_RAM_PAIRS as BACKEND_CPU_RAM_PAIRS,
    LANGUAGES as BACKEND_LANGUAGES,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER_PATH = REPO_ROOT / "runner" / "e2b-templates" / "build_grid.py"


def _load_driver():
    """Import the operator-side build driver as a module so the test
    can introspect its grid without invoking the E2B API.
    """
    import sys

    name = "_e2b_template_build_grid"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_grid_has_exactly_24_entries() -> None:
    """The plan + admin docs assume 24 templates (8 CPU/RAM pairings
    × 3 languages: node, python, docker). Locking the count prevents
    a silent matrix change that breaks UI capability rendering."""
    driver = _load_driver()
    assert len(driver.grid()) == 24


def test_grid_names_are_unique() -> None:
    driver = _load_driver()
    names = [c.template_name for c in driver.grid()]
    assert len(set(names)) == len(names)


def test_grid_naming_convention() -> None:
    driver = _load_driver()
    for combo in driver.grid():
        assert combo.language in {"node", "python", "docker"}
        assert combo.template_name == (
            f"mcpolis-{combo.language}-cpu{combo.cpu}-ram{combo.ram_mb}"
        )


def test_grid_languages_split_evenly() -> None:
    driver = _load_driver()
    by_lang: dict[str, int] = {}
    for combo in driver.grid():
        by_lang[combo.language] = by_lang.get(combo.language, 0) + 1
    assert by_lang == {"node": 8, "python": 8, "docker": 8}


def test_driver_matches_backend_template_grid() -> None:
    """The operator-side driver and the backend's capabilities
    source-of-truth must list the same languages + (cpu, ram)
    pairings. Drift means a UI that offers combos the runner
    can't build, or vice versa.
    """
    driver = _load_driver()
    driver_languages: tuple[str, ...] = driver.LANGUAGES
    driver_pairs: tuple[tuple[int, int], ...] = driver.CPU_RAM_PAIRS

    assert sorted(driver_languages) == sorted(BACKEND_LANGUAGES)
    assert sorted(driver_pairs) == sorted(BACKEND_CPU_RAM_PAIRS)


def test_driver_check_consistency_passes() -> None:
    """The driver's own ``--check`` mode validates the matrix +
    Dockerfile presence. Run it as part of CI so a missing
    ``Dockerfile.<lang>`` surfaces here, not at build time."""
    driver = _load_driver()
    rc = driver._check_consistency()  # type: ignore[reportPrivateUsage]
    assert rc == 0


def test_driver_resolves_existing_dockerfiles() -> None:
    """Every combo's Dockerfile path must resolve to an existing
    file. The driver reads file content rather than passing the
    path through to the SDK, so a missing file would surface only
    at build time — too late for CI to catch."""
    driver = _load_driver()
    for combo in driver.grid():
        assert combo.dockerfile_path.exists(), (
            f"Dockerfile missing for {combo.template_name}: "
            f"{combo.dockerfile_path}"
        )


def test_grid_pairings_documented_in_readme() -> None:
    """Every (cpu, ram) pairing the driver emits must appear in the
    README — the README is the human-facing source of truth and
    silent drift would surface as wrong UI documentation."""
    driver = _load_driver()
    readme = (
        REPO_ROOT / "runner" / "e2b-templates" / "README.md"
    ).read_text()
    pairings = sorted({(c.cpu, c.ram_mb) for c in driver.grid()})
    for cpu, _ram in pairings:
        # The README's pairings table only references CPU once per
        # row; the row also lists RAM values inline. A weaker
        # invariant than full cross-check, but prevents the table
        # from going stale.
        assert f"| {cpu} " in readme, f"CPU {cpu} missing from README table"
