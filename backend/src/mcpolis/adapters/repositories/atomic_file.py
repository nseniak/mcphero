"""Saving a file so a write cut short never leaves half of it.

The file-backed (standalone) stores save by rewriting a whole JSON
file. Written in place, a save cut short (a crash, a full disk) leaves
half a file; the store's reader turns that into "empty", and the next
save erases everything the file held. ``write_text_atomic`` writes a
temp file beside the target and renames it over the target, which the
operating system does in one step: a reader sees the old file or the
new one, never a half.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


def write_text_atomic(
    path: Path, text: str, *, tmp_path: Path | None = None,
) -> None:
    """Replace ``path``'s content with ``text`` in one step.

    The temp file is ``path``'s full name plus ``.tmp`` (``a.json`` ->
    ``a.json.tmp``), so two files of one folder that share a stem
    (``a.json``, ``a.jsonl``) never write the same temp file. ``tmp_path``
    names another one (the audit log uses a hidden name its rotated-file
    glob can't match). The parent folder is created when missing.

    The new file keeps the permissions of the file it replaces (an
    operator's ``chmod 600`` on a file of sign-in tokens survives the
    save), and its content is flushed to disk before the rename."""
    tmp = (
        tmp_path if tmp_path is not None
        else path.with_name(f"{path.name}.tmp")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        tmp.chmod(stat.S_IMODE(path.stat().st_mode))
    except FileNotFoundError:
        pass  # a first save: the new file keeps the default mode
    tmp.replace(path)
