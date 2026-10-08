#!/usr/bin/env python3
"""Pick the folder a test run writes its logs and reports into.

Every test runner (``make test-all``, the e2e, unit, integration, broad
matrix and frontend runners) writes into a folder of its own, so two runs
at the same time, from one checkout or several, never overwrite each
other's results. Before this, all runs shared fixed ``/tmp/mcpolis-*``
paths. On 2026-10-07 two worktrees ran ``make test-all`` together; one
run's e2e leg failed, but ``/tmp/mcpolis-all-e2e.log`` already held the
OTHER run's green output, so the failure could not be diagnosed.

    MCPOLIS_TEST_OUT_DIR set  -> that folder. ``make test-all`` sets it
                                 for each of its legs, so one test-all
                                 run keeps every leg's files together.
    otherwise                 -> a new folder under RUNS_ROOT, named
                                 <date>-<time>-<suite>-<checkout>-<random>

The folder is returned as a real path, never through a link (on macOS
``/tmp`` prints as ``/private/tmp``). A run must not write through a
``latest-*`` link: another run may move it, and pointing the variable at
one used to turn the link into a loop that crashed every later run.

When the folder is inside RUNS_ROOT, ``RUNS_ROOT/latest-<suite>`` is
pointed at it as the run starts. The link is a convenience for a lone run:
with two runs at once it belongs to whichever started last, so read the
folder the run printed instead. A folder you pick outside RUNS_ROOT
leaves the shared links alone, since they would dangle once you delete it.

Making a new folder also deletes old ones. A folder goes once it is older
than ``MIN_AGE_BEFORE_DELETE`` AND at least ``KEEP_RUNS`` newer folders
exist AND nothing in it changed for ``MIN_AGE_BEFORE_DELETE``. The age
floors matter: agents start many small unit runs, and a count alone would
let a burst of them delete the folder of a long run that is still going.

From bash::

    RUN_DIR="$(python3 tests/run_folder.py unit)"
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

RUNS_ROOT = Path("/tmp/mcpolis-test-runs")
OUT_DIR_ENV = "MCPOLIS_TEST_OUT_DIR"
KEEP_RUNS = 30
MIN_AGE_BEFORE_DELETE = timedelta(days=1)

# The checkout (main clone or worktree) this file belongs to. Named in
# each folder so runs from different worktrees are told apart at a glance.
CHECKOUT = Path(__file__).resolve().parent.parent.name

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_RUN_FOLDER_NAME = re.compile(r"^(\d{8}-\d{6})-")
_SUITE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def resolve_run_folder(
    suite: str,
    *,
    env: Mapping[str, str] = os.environ,
    root: Path = RUNS_ROOT,
    now: datetime | None = None,
) -> Path:
    """Return this run's folder, creating it, and point
    ``root/latest-<suite>`` at it."""
    if not _SUITE_NAME.match(suite):
        raise ValueError(f"bad suite name {suite!r}")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    explicit = env.get(OUT_DIR_ENV, "").strip()
    if explicit:
        given = Path(explicit).expanduser()
        given.mkdir(parents=True, exist_ok=True)
        folder = given.resolve()
    else:
        started = now or datetime.now()
        # mkdtemp appends a random part, so two runs started in the same
        # second still get two folders.
        folder = Path(tempfile.mkdtemp(
            prefix=f"{started.strftime(_STAMP_FORMAT)}-{suite}-{CHECKOUT}-",
            dir=root,
        ))
        prune_old_run_folders(root, now=started)
    if folder.is_relative_to(root):
        _point_latest(root, suite, folder)
    return folder


def prune_old_run_folders(
    root: Path, *, now: datetime, keep: int = KEEP_RUNS,
) -> list[Path]:
    """Delete the run folders beyond the newest ``keep`` that started
    over ``MIN_AGE_BEFORE_DELETE`` ago and were not written to since.
    Never deletes a folder a ``latest-*`` link points at. Returns the
    deleted folders."""
    root = root.resolve()
    protected: set[Path] = set()
    for link in root.glob("latest-*"):
        try:
            protected.add(link.resolve(strict=True))
        except (OSError, RuntimeError):
            # A dangling or looping link protects nothing; the next run
            # of its suite points it somewhere real again.
            continue
    runs: list[tuple[datetime, str, Path]] = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        match = _RUN_FOLDER_NAME.match(entry.name)
        if match is None:
            continue
        try:
            started = datetime.strptime(match.group(1), _STAMP_FORMAT)
        except ValueError:
            continue
        runs.append((started, entry.name, entry))
    runs.sort(reverse=True)
    deleted: list[Path] = []
    for started, _name, entry in runs[keep:]:
        if now - started < MIN_AGE_BEFORE_DELETE:
            continue
        if entry in protected or _written_since(entry, now - MIN_AGE_BEFORE_DELETE):
            continue
        # Another run may be pruning the same folder right now.
        shutil.rmtree(entry, ignore_errors=True)
        deleted.append(entry)
    return deleted


def _written_since(folder: Path, moment: datetime) -> bool:
    """True if the folder or anything in it changed after ``moment``: a
    run that has gone on for over a day (a watch session, a laptop asleep
    mid-run) may still be writing there."""
    cutoff = moment.timestamp()
    try:
        return any(
            path.lstat().st_mtime > cutoff
            for path in (folder, *folder.rglob("*"))
        )
    except OSError:
        return True


def _point_latest(root: Path, suite: str, folder: Path) -> None:
    """Swap ``root/latest-<suite>`` to ``folder`` in one step, so a reader
    sees the old link or the new one, never none. The link is only a
    convenience, so a failure here warns instead of failing the run."""
    link = root / f"latest-{suite}"
    temp_link = root / f".latest-{suite}.{os.getpid()}"
    try:
        with contextlib.suppress(FileNotFoundError):
            temp_link.unlink()
        temp_link.symlink_to(folder)
        os.replace(temp_link, link)
    except OSError as e:
        print(f"[run_folder] could not point {link} at {folder}: {e}",
              file=sys.stderr)


def main(
    argv: list[str],
    *,
    env: Mapping[str, str] = os.environ,
    root: Path = RUNS_ROOT,
) -> int:
    """``run_folder.py <suite>``: print this run's folder."""
    if len(argv) != 1:
        print("usage: run_folder.py <suite>", file=sys.stderr)
        return 2
    try:
        folder = resolve_run_folder(argv[0], env=env, root=root)
    except (ValueError, OSError, RuntimeError) as e:
        print(f"run_folder.py: {e}", file=sys.stderr)
        return 1
    print(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
