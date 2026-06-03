"""Per-MCP template variables referenced from upstream configs as ``${NAME}``.

The bucket holds two flavours:

- ``is_secret=True`` (the default): the UI obfuscates the value by
  default (1Password-style: ``••••`` placeholder with an eye toggle
  to reveal). Designed for credentials.
- ``is_secret=False``: value is shown in clear in the UI. Designed
  for non-sensitive configuration like feature flags.

Both flavours return the plaintext ``value`` from
:meth:`TemplateVarRepository.list_summaries` — the dashboard SPA is
admin-only and the value is encrypted at rest (cloud) or stored
plaintext alongside other dev artefacts (standalone). The
``is_secret`` flag drives **display** style (obfuscate-by-default vs
verbatim), not server-side filtering.
"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

# Standard env-var convention: uppercase letter or underscore start,
# uppercase letters / digits / underscore body. Same regex used by the
# substitution helper at the resolution boundary, so a name accepted
# here is exactly a name the helper will recognise.
_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Threshold above which a saved secret's ``last_four`` preview is
# stored. Picked to keep the leak ratio acceptable on modern API
# tokens — see plan §"Secret storage" for the math (16 → 76% unknown,
# 11 → 64% unknown).
#
# Only meaningful for ``is_secret=True`` rows; plain rows ignore it
# (their value is shown verbatim in the UI).
LAST_FOUR_MIN_LENGTH = 16


def compute_last_four(value: str) -> str | None:
    """Last 4 chars when the value is long enough to make the preview safe."""
    if len(value) > LAST_FOUR_MIN_LENGTH:
        return value[-4:]
    return None


class MissingTemplateVarError(Exception):
    """Raised by the substitution helper when ``${NAME}`` is unresolved.

    The repository didn't have an env var named ``name`` for the
    upstream referenced by ``upstream_id``. Surfaced to the user as a
    clear "Environment variable '<name>' is referenced by upstream
    '<id>' but not defined" error at session start.
    """

    def __init__(self, upstream_id: str, name: str) -> None:
        self.upstream_id = upstream_id
        self.name = name
        super().__init__(
            f"Environment variable {name!r} is referenced by upstream "
            f"{upstream_id!r} but not defined"
        )


class TemplateVar(BaseModel):
    """A per-MCP environment variable with the plaintext value attached.

    Constructed in flight only — by the admin tools at write time, by
    the repositories during ``get_value``. Never serialised back to the
    UI; only :class:`TemplateVarSummary` crosses that boundary.

    ``is_secret`` is set at create time and is immutable thereafter
    (the repository ignores changes to this flag on replace).
    """

    name: str
    value: str
    is_secret: bool = True
    last_four: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"Env var name {v!r} must match {_NAME_RE.pattern}"
            )
        return v


class TemplateVarSummary(BaseModel):
    """View of a template variable — sent to the dashboard SPA.

    Both ``is_secret=True`` and ``is_secret=False`` rows carry the
    plaintext ``value``. The flag drives **display** style only —
    secret rows obfuscate by default with an eye toggle to reveal
    (1Password-style); plain rows render verbatim.

    The dashboard API is admin-scoped; encryption-at-rest still
    applies uniformly in cloud mode regardless of the flag.
    ``last_four`` is preserved for the masked preview (used as the
    obfuscation placeholder when the value is long enough).
    """

    name: str
    is_secret: bool = True
    value: str | None = None
    last_four: str | None = None
    created_at: datetime
    updated_at: datetime


def is_valid_template_var_name(name: str) -> bool:
    """Public helper so callers don't have to import the private regex."""
    return bool(_NAME_RE.match(name))
