"""Where the integration suite finds its ``.env.test`` (E2B key).

The file is gitignored, so a git worktree has none of its own: without a
fallback every paid test there skips, and ``make test-all`` still
reports the integration leg as passed (51 skipped, 2026-10-08). The
fallback is the same file in the main checkout the worktree belongs to.
"""

from __future__ import annotations

from pathlib import Path

ENV_FILE_NAME = ".env.test"


def main_checkout_root(checkout_root: Path) -> Path | None:
    """The main checkout of the git worktree at ``checkout_root``.

    A worktree's ``.git`` is a file reading ``gitdir: <main>/.git/
    worktrees/<name>``; the main checkout's is a directory. ``None`` when
    ``checkout_root`` is not a worktree (or the file can't be read).
    """
    dot_git = checkout_root / ".git"
    if not dot_git.is_file():
        return None
    try:
        first_line = dot_git.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    prefix = "gitdir:"
    if not first_line.startswith(prefix):
        return None
    gitdir = Path(first_line[len(prefix):].strip())
    if not gitdir.is_absolute():
        gitdir = (checkout_root / gitdir).resolve()
    # <main>/.git/worktrees/<name> -> <main>
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        return None
    return gitdir.parent.parent.parent


def default_env_file(integration_dir: Path, checkout_root: Path) -> Path:
    """This checkout's ``.env.test``, else the main checkout's one."""
    own = integration_dir / ENV_FILE_NAME
    if own.is_file():
        return own
    main_root = main_checkout_root(checkout_root)
    if main_root is None:
        return own
    shared = main_root / integration_dir.relative_to(checkout_root) / ENV_FILE_NAME
    return shared if shared.is_file() else own
