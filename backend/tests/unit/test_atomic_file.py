"""``write_text_atomic``: the one save every file-backed store uses.

A save that fails part way must leave the previous file whole, because
the stores read a damaged file as empty and the next save would then
erase everything it held.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.atomic_file import write_text_atomic


def make_saved_file(tmp_path: Path, text: str = '{"kept": true}') -> Path:
    path = tmp_path / "store" / "data.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_save_replaces_the_content_and_leaves_no_temp_file(
    tmp_path: Path,
) -> None:
    path = make_saved_file(tmp_path)

    write_text_atomic(path, '{"new": true}')

    assert path.read_text() == '{"new": true}'
    assert sorted(p.name for p in path.parent.iterdir()) == ["data.json"]


def test_save_creates_a_missing_folder(tmp_path: Path) -> None:
    path = tmp_path / "not-yet" / "data.json"

    write_text_atomic(path, "{}")

    assert path.read_text() == "{}"


def test_a_save_that_fails_part_way_leaves_the_old_file_whole(
    tmp_path: Path,
) -> None:
    """The temp file can't be written (here: a folder sits at its
    name), so the save fails before it touches the real file."""
    path = make_saved_file(tmp_path)
    blocked_tmp = path.with_name(f"{path.name}.tmp")
    blocked_tmp.mkdir()

    with pytest.raises(OSError):
        write_text_atomic(path, '{"half": ')

    assert path.read_text() == '{"kept": true}'


def test_a_named_temp_file_is_used_instead_of_the_default(
    tmp_path: Path,
) -> None:
    """The audit log names its own hidden temp file, so its rotated-file
    glob never matches a temp file."""
    path = make_saved_file(tmp_path)
    hidden_tmp = path.with_name(f".{path.name}.tmp")
    path.with_name(f"{path.name}.tmp").mkdir()  # the default name would fail

    write_text_atomic(path, "rows", tmp_path=hidden_tmp)

    assert path.read_text() == "rows"
    assert not hidden_tmp.exists()


def test_two_files_sharing_a_stem_use_different_temp_files(
    tmp_path: Path,
) -> None:
    """``data.json`` and ``data.jsonl`` in one folder: while a temp file
    named after ``data.json`` is in the way (a folder sits at each name it
    could take, ``data.tmp`` or ``data.json.tmp``), ``data.jsonl`` still
    saves, because its temp file is named after its own full name."""
    json_file = make_saved_file(tmp_path)
    jsonl_file = json_file.with_suffix(".jsonl")
    json_file.with_suffix(".tmp").mkdir()
    json_file.with_name(f"{json_file.name}.tmp").mkdir()

    write_text_atomic(jsonl_file, "line")

    assert jsonl_file.read_text() == "line"
    assert json_file.read_text() == '{"kept": true}'


def test_save_keeps_the_permissions_of_the_file_it_replaces(
    tmp_path: Path,
) -> None:
    """An operator who made a file of sign-in tokens private
    (``chmod 600``) keeps it private after the next save."""
    path = make_saved_file(tmp_path)
    path.chmod(0o600)

    write_text_atomic(path, "{}")

    assert path.stat().st_mode & 0o777 == 0o600
