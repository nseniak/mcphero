"""Per-MCP template variables referenced from upstream configs as ``${NAME}``.

The bucket holds two flavours:

- ``is_secret=True`` (the default): a **password**. Write-only: once
  saved, the value never leaves the backend again. Summaries carry
  only ``has_value`` (set / empty). The only plaintext read path is
  :meth:`TemplateVarRepository.get_value`, used by substitution at
  session start.
- ``is_secret=False``: a plain variable. Summaries carry the value so
  the UI can render it verbatim (feature flags, regions, ...).

:func:`make_template_var_summary` is the single place that decides
what a summary may carry; both repositories build summaries through
it, so no caller of ``list_summaries`` can see a password.
"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel

# Standard env-var convention: uppercase letter or underscore start,
# uppercase letters / digits / underscore body. Same regex used by the
# substitution helper at the resolution boundary, so a name accepted
# here is exactly a name the helper will recognise.
_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

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


class TemplateVarSummary(BaseModel):
    """View of a template variable — sent to the dashboard SPA.

    ``value`` is always ``None`` for a password (``is_secret=True``);
    ``has_value`` is the only thing a password row says about its
    value. Build instances with :func:`make_template_var_summary`.
    """

    name: str
    is_secret: bool = True
    value: str | None = None
    has_value: bool = False
    created_at: datetime
    updated_at: datetime


def make_template_var_summary(
    *,
    name: str,
    is_secret: bool,
    stored_value: str | None,
    created_at: datetime,
    updated_at: datetime,
) -> TemplateVarSummary:
    """Build a summary, dropping the value of a password.

    ``has_value`` is ``False`` for an empty (or missing) value, so the
    UI can tell "set" from "empty" without seeing the value.
    """
    return TemplateVarSummary(
        name=name,
        is_secret=is_secret,
        value=None if is_secret else stored_value,
        has_value=bool(stored_value),
        created_at=created_at,
        updated_at=updated_at,
    )


def is_valid_template_var_name(name: str) -> bool:
    """Public helper so callers don't have to import the private regex."""
    return bool(_NAME_RE.match(name))
