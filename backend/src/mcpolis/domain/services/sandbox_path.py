"""Confinement of materialize-file target paths to the sandbox home.

``MaterializeFile.target_path`` is operator-controlled: it carries the
``${HOME}/${FILE_NAME}`` substitution result, and the config-time
validator (``SandboxFile._validate_target_path``) deliberately ALLOWS an
absolute or ``${...}``-prefixed path. At materialize time the RESOLVED
path must still land inside the session's sandbox home — a ``..``
traversal (``${HOME}/../../etc/x``) or an absolute system path
(``/etc/x``) that escapes the home is rejected before any write happens.

On E2B such a write would land outside ``${HOME}`` (inside the org's own
isolated sandbox — a least-surprise / defense-in-depth gap, not a
cross-tenant escape); on the dev-only ``local-subprocess`` backend it
would write a real file on the gateway host outside the per-session temp
home. Both sandbox backends call this one helper so the confinement rule
lives in a single place (DRY) rather than duplicated per backend.
"""
from __future__ import annotations

import os


class SandboxFilePathError(ValueError):
    """A materialize ``target_path`` escapes the session's sandbox home."""


def confine_to_sandbox_home(target_path: str, sandbox_home: str) -> str:
    """Return the normalized absolute path for *target_path* after
    verifying it resolves inside *sandbox_home*.

    A relative *target_path* is resolved against *sandbox_home*. Raises
    :class:`SandboxFilePathError` when the normalized path escapes the
    home — a ``..`` traversal that climbs above it, an absolute path
    elsewhere, or a sibling that merely shares a name prefix
    (``/home/user-evil`` for home ``/home/user``).
    """
    home_norm = os.path.normpath(sandbox_home)
    resolved = (
        target_path
        if os.path.isabs(target_path)
        else os.path.join(home_norm, target_path)
    )
    path_norm = os.path.normpath(resolved)
    try:
        confined = os.path.commonpath([home_norm, path_norm]) == home_norm
    except ValueError:
        # Mixed absolute/relative or different drives (Windows): treat as
        # an escape rather than risk a false "confined".
        confined = False
    if not confined:
        raise SandboxFilePathError(
            f"materialize target_path {target_path!r} escapes the sandbox "
            f"home {sandbox_home!r}"
        )
    return path_norm


__all__ = ["SandboxFilePathError", "confine_to_sandbox_home"]
