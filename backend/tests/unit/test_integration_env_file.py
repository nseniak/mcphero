"""The integration suite finds its key file from a git worktree too."""
from __future__ import annotations

from pathlib import Path

from tests.integration._env_file import (
    ENV_FILE_NAME,
    default_env_file,
    main_checkout_root,
)

INTEGRATION = Path("backend/tests/integration")


def make_main_checkout(root: Path, *, with_key_file: bool) -> Path:
    (root / ".git" / "worktrees").mkdir(parents=True)
    (root / INTEGRATION).mkdir(parents=True)
    if with_key_file:
        (root / INTEGRATION / ENV_FILE_NAME).write_text("MCPOLIS_E2B_API_KEY=x\n")
    return root


def make_worktree(main: Path, path: Path, name: str = "wt") -> Path:
    gitdir = main / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (path / INTEGRATION).mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {gitdir}\n")
    return path


def test_a_worktree_without_a_key_file_uses_the_main_checkouts(tmp_path: Path) -> None:
    main = make_main_checkout(tmp_path / "main", with_key_file=True)
    wt = make_worktree(main, tmp_path / "wt")

    found = default_env_file(wt / INTEGRATION, wt)

    assert found == main / INTEGRATION / ENV_FILE_NAME


def test_a_worktrees_own_key_file_wins(tmp_path: Path) -> None:
    main = make_main_checkout(tmp_path / "main", with_key_file=True)
    wt = make_worktree(main, tmp_path / "wt")
    (wt / INTEGRATION / ENV_FILE_NAME).write_text("MCPOLIS_E2B_API_KEY=y\n")

    assert default_env_file(wt / INTEGRATION, wt) == wt / INTEGRATION / ENV_FILE_NAME


def test_the_main_checkout_keeps_its_own_path(tmp_path: Path) -> None:
    main = make_main_checkout(tmp_path / "main", with_key_file=False)

    assert main_checkout_root(main) is None
    assert default_env_file(main / INTEGRATION, main) == main / INTEGRATION / ENV_FILE_NAME


def test_no_key_file_anywhere_points_at_the_worktrees_own_path(tmp_path: Path) -> None:
    main = make_main_checkout(tmp_path / "main", with_key_file=False)
    wt = make_worktree(main, tmp_path / "wt")

    assert default_env_file(wt / INTEGRATION, wt) == wt / INTEGRATION / ENV_FILE_NAME


def test_a_git_file_that_is_not_a_worktree_link_is_ignored(tmp_path: Path) -> None:
    checkout = tmp_path / "sub"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: /somewhere/.git/modules/sub\n")

    assert main_checkout_root(checkout) is None
