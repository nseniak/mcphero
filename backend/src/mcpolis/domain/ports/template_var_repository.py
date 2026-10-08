"""Port for the per-MCP environment-variable store.

Two implementations:

- :class:`mcpolis.adapters.repositories.file_template_var_repository.FileTemplateVarRepository`
  for standalone mode (plaintext JSON on the user's machine, same
  threat model as ``mcp.json``).
- :class:`mcpolis.adapters.repositories.mongo_template_var_repository.MongoTemplateVarRepository`
  for cloud mode (AES-256-GCM via the existing
  :class:`mcpolis.adapters.repositories.encryption.FieldEncryptor`).

Domain code only sees this Protocol — never ciphertext, never the
file path, never raw Mongo docs.

The bucket holds both secret and non-secret env vars; the
``is_secret`` flag is set at create time and is **immutable**
thereafter (the repository implementations ignore the flag on
replace and preserve the existing value).
"""
from __future__ import annotations

from typing import Protocol

from mcpolis.domain.model.template_var import TemplateVarSummary


class TemplateVarRepository(Protocol):
    async def list_summaries(
        self, org_id: str, upstream_id: str
    ) -> list[TemplateVarSummary]:
        """All defined template variables for one upstream.

        Sorted by name ascending so the UI's order is stable across
        calls. Returns an empty list when nothing is defined.

        Passwords (``is_secret=True``) are write-only: their summary
        carries ``value=None`` and only ``has_value``. Plain rows carry
        the value. Build summaries with
        :func:`~mcpolis.domain.model.template_var.make_template_var_summary`.
        """
        ...

    async def get_value(
        self, org_id: str, upstream_id: str, name: str
    ) -> str | None:
        """Plaintext value for a single env var, or ``None`` if absent.

        Called by the substitution layer at sandbox-launch time. Do
        NOT pass the result to any logging call site. Works the same
        for secret and non-secret rows — substitution doesn't care.
        """
        ...

    async def set(
        self,
        org_id: str,
        upstream_id: str,
        name: str,
        value: str,
        *,
        is_secret: bool = True,
    ) -> TemplateVarSummary:
        """Create or replace an env var; return the post-write summary.

        On **create**, the ``is_secret`` flag is honoured and stored.
        On **replace** (a row with this ``name`` already exists), the
        existing record's ``is_secret`` wins — the caller's flag is
        ignored. This pins the contract that secrecy is a create-time
        decision: to flip a value's secrecy after the fact, the caller
        must delete + re-create.

        The returned summary follows the same write-only rule as
        :meth:`list_summaries`: a password's value is not in it.
        """
        ...

    async def delete(
        self, org_id: str, upstream_id: str, name: str
    ) -> None:
        """Remove a single env var. Idempotent — missing → no-op."""
        ...

    async def delete_all(
        self, org_id: str, upstream_id: str
    ) -> None:
        """Cascade delete on upstream removal. Idempotent."""
        ...

    async def delete_all_for_org(self, org_id: str) -> int:
        """Remove every template variable for the org, across all
        upstreams (org-deletion cascade). Returns the number of rows
        removed. Idempotent."""
        ...
