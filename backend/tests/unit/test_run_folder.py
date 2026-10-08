"""Each test run gets its own results folder (``tests/run_folder.py``).

The test runners used to share fixed ``/tmp/mcpolis-*`` paths, so two
runs at once overwrote each other's logs and reports. These tests lock the
folder rule every runner relies on: a fresh folder per run (or the one
``make test-all`` hands its legs), a ``latest-<suite>`` link, and a
clean-up that never deletes a folder a run may still be writing.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = REPO_ROOT / "tests" / "run_folder.py"

NOW = datetime(2026, 10, 7, 14, 30, 12)


def _load_run_folder() -> ModuleType:
    """Import the repo-level helper as a module (it lives beside the
    runners in ``tests/``, outside the backend package)."""
    name = "_mcpolis_run_folder"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_run_folders(
    root: Path, count: int, first: datetime, step: timedelta,
    suite: str = "unit",
) -> list[Path]:
    """Create ``count`` run folders named the way the helper names them,
    oldest first, each last written at the time its name says."""
    root.mkdir(parents=True, exist_ok=True)
    folders: list[Path] = []
    for i in range(count):
        started = first + step * i
        folder = root / f"{started:%Y%m%d-%H%M%S}-{suite}-somecheckout-r{i:03d}"
        folder.mkdir()
        os.utime(folder, (started.timestamp(), started.timestamp()))
        folders.append(folder)
    return folders


def names(paths: list[Path]) -> list[str]:
    return sorted(p.name for p in paths)


def test_a_new_folder_is_made_under_the_root_and_named_for_the_run() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        folder = rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        assert folder.is_dir()
        assert folder.parent == root.resolve()
        assert folder.name.startswith(f"20261007-143012-unit-{rf.CHECKOUT}-")


def test_two_runs_started_in_the_same_second_get_two_folders() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = rf.resolve_run_folder("all", env={}, root=root, now=NOW)
        second = rf.resolve_run_folder("all", env={}, root=root, now=NOW)
        assert first != second
        assert first.is_dir() and second.is_dir()


def test_a_folder_given_in_the_env_is_used_and_created() -> None:
    """``make test-all`` hands its own folder to each leg this way."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        given = Path(tmp) / "given" / "nested"
        folder = rf.resolve_run_folder(
            "e2e", env={rf.OUT_DIR_ENV: str(given)}, root=root, now=NOW,
        )
        assert folder == given.resolve()
        assert given.is_dir()


def test_a_folder_outside_the_root_leaves_the_shared_links_alone() -> None:
    """A folder someone picks outside the root would leave the shared
    ``latest-*`` link dangling once they delete it."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        newest = rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        rf.resolve_run_folder(
            "unit", env={rf.OUT_DIR_ENV: str(Path(tmp) / "mine")},
            root=root, now=NOW,
        )
        assert (root / "latest-unit").resolve() == newest


def test_latest_link_points_at_the_newest_run_of_its_suite() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        newer_unit = rf.resolve_run_folder(
            "unit", env={}, root=root, now=NOW + timedelta(minutes=1),
        )
        e2e = rf.resolve_run_folder("e2e", env={}, root=root, now=NOW)
        assert (root / "latest-unit").resolve() == newer_unit
        assert (root / "latest-e2e").resolve() == e2e


def test_a_leg_given_a_folder_also_moves_its_latest_link() -> None:
    """After ``make test-all``, ``latest-unit`` must reach the unit report
    that the unit leg wrote into test-all's folder."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        all_folder = rf.resolve_run_folder("all", env={}, root=root, now=NOW)
        rf.resolve_run_folder(
            "unit", env={rf.OUT_DIR_ENV: str(all_folder)}, root=root, now=NOW,
        )
        assert (root / "latest-unit").resolve() == all_folder
        assert (root / "latest-all").resolve() == all_folder


def test_pointing_the_env_at_a_latest_link_writes_into_the_real_folder() -> None:
    """The docs point readers at ``latest-<suite>``. Given that link as the
    folder, a run used to point the link at itself, and the loop then
    crashed every later run on the host."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        folder = rf.resolve_run_folder(
            "unit", env={rf.OUT_DIR_ENV: str(root / "latest-unit")},
            root=root, now=NOW,
        )
        assert folder == first
        assert (root / "latest-unit").resolve(strict=True) == first
        later = rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        assert (root / "latest-unit").resolve(strict=True) == later


def test_a_broken_latest_link_does_not_stop_later_runs() -> None:
    """A looping or dangling link (left by an older version, or by hand)
    must not crash the clean-up; the next run of its suite repairs it."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "latest-unit").symlink_to(root / "latest-unit")
        (root / "latest-e2e").symlink_to(root / "gone")
        folder = rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        assert (root / "latest-unit").resolve(strict=True) == folder


def test_cleanup_keeps_the_newest_folders_and_deletes_older_ones() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = make_run_folders(
            root, 35, NOW - timedelta(days=30), timedelta(minutes=1),
        )
        deleted = rf.prune_old_run_folders(root, now=NOW, keep=30)
        assert names(deleted) == names(old[:5])
        assert [f.exists() for f in old] == [False] * 5 + [True] * 30


def test_a_new_folder_triggers_the_cleanup() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = make_run_folders(
            root, rf.KEEP_RUNS, NOW - timedelta(days=30), timedelta(minutes=1),
        )
        rf.resolve_run_folder("unit", env={}, root=root, now=NOW)
        # One new folder pushes exactly the oldest one past the limit.
        assert not old[0].exists()
        assert all(f.exists() for f in old[1:])


def test_cleanup_never_deletes_a_folder_younger_than_a_day() -> None:
    """A burst of small unit runs must not delete the folder of a long
    run (an integration leg, a test-all) that is still being written."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        recent = make_run_folders(
            root, 40, NOW - timedelta(hours=23), timedelta(minutes=1),
        )
        assert rf.prune_old_run_folders(root, now=NOW, keep=30) == []
        assert all(f.exists() for f in recent)


def test_cleanup_keeps_an_old_folder_that_is_still_being_written() -> None:
    """A run older than a day by its name (a watch session, a laptop
    asleep mid-run) is kept while it still writes there."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = make_run_folders(
            root, 35, NOW - timedelta(days=30), timedelta(minutes=1),
        )
        log = old[0] / "unit-report.json"
        log.write_text("{}")
        os.utime(log, (NOW.timestamp(), NOW.timestamp()))
        deleted = rf.prune_old_run_folders(root, now=NOW, keep=30)
        assert old[0].exists()
        assert names(deleted) == names(old[1:5])


def test_cleanup_never_deletes_the_folder_a_latest_link_points_at() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = make_run_folders(
            root, 35, NOW - timedelta(days=30), timedelta(minutes=1),
        )
        (root / "latest-integration").symlink_to(old[0])
        deleted = rf.prune_old_run_folders(root, now=NOW, keep=30)
        assert old[0].exists()
        assert names(deleted) == names(old[1:5])


def test_cleanup_leaves_entries_it_did_not_make_alone() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_run_folders(root, 3, NOW - timedelta(days=30), timedelta(minutes=1))
        notes = root / "notes"
        notes.mkdir()
        stray_file = root / "20200101-000000-unit-file"
        stray_file.write_text("not a folder")
        assert rf.prune_old_run_folders(root, now=NOW, keep=0) != []
        assert notes.is_dir()
        assert stray_file.is_file()


def test_a_run_given_a_folder_does_not_clean_up() -> None:
    """Only the run that makes a folder cleans up: test-all's legs must
    not delete anything while test-all's own folder is in use."""
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        old = make_run_folders(
            root, 35, NOW - timedelta(days=30), timedelta(minutes=1),
        )
        rf.resolve_run_folder(
            "unit", env={rf.OUT_DIR_ENV: str(Path(tmp) / "given")},
            root=root, now=NOW,
        )
        assert all(f.exists() for f in old)


@pytest.mark.parametrize("suite", ["", "Unit", "../escape", "unit/x", "-x"])
def test_a_bad_suite_name_is_refused(suite: str) -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError):
            rf.resolve_run_folder(suite, env={}, root=Path(tmp), now=NOW)


def test_the_command_line_prints_the_folder() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        given = Path(tmp) / "given"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = rf.main(
                ["vitest"], env={rf.OUT_DIR_ENV: str(given)},
                root=Path(tmp) / "runs",
            )
        assert rc == 0
        assert out.getvalue() == f"{given.resolve()}\n"


def test_the_command_line_needs_exactly_one_suite() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        assert rf.main([], env={}, root=Path(tmp)) == 2
        assert rf.main(["unit", "e2e"], env={}, root=Path(tmp)) == 2
        assert rf.main(["../escape"], env={}, root=Path(tmp)) == 1


def test_the_command_line_reports_a_looping_folder_instead_of_crashing() -> None:
    rf = _load_run_folder()
    with tempfile.TemporaryDirectory() as tmp:
        loop = Path(tmp) / "loop"
        loop.symlink_to(loop)
        assert rf.main(
            ["unit"], env={rf.OUT_DIR_ENV: str(loop)}, root=Path(tmp) / "runs",
        ) == 1
