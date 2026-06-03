"""Port for the per-MCP **Sandbox files** store.

Two implementations:

- :class:`mcpolis.adapters.repositories.file_sandbox_file_repository.FileSandboxFileRepository`
  for standalone mode (plaintext JSON on the user's machine, same
  threat model as ``mcp.json``).
- :class:`mcpolis.adapters.repositories.mongo_sandbox_file_repository.MongoSandboxFileRepository`
  for cloud mode (AES-256-GCM via the existing
  :class:`mcpolis.adapters.repositories.encryption.FieldEncryptor`).

Domain code only sees this Protocol — never ciphertext, never the
file path on disk, never raw Mongo docs.

File names live in their own namespace — they don't participate in
``${...}`` substitution, so a file named ``GCP_CRED`` and a user
Variable named ``GCP_CRED`` coexist with no conflict.
"""
from __future__ import annotations

from typing import Protocol

from mcpolis.domain.model.sandbox_file import SandboxFile, SandboxFileSummary


class SandboxFileRepository(Protocol):
    async def list_summaries(
        self, org_id: str, upstream_id: str
    ) -> list[SandboxFileSummary]:
        """All defined sandbox files for one upstream.

        Sorted by name ascending so the UI's order is stable across
        calls. Returns an empty list when nothing is defined.

        ``contents`` is **never** included — only metadata. The
        upload page renders size + sha256, not the body.
        """
        ...

    async def list_full(
        self, org_id: str, upstream_id: str
    ) -> list[SandboxFile]:
        """Every sandbox file with contents attached, for the launcher.

        Called at sandbox-launch time so the pre-exec hook can write
        each file into the sandbox via ``files.write``. Do NOT log
        the result — ``contents`` is plaintext and may include
        credentials.
        """
        ...

    async def get(
        self, org_id: str, upstream_id: str, name: str
    ) -> SandboxFile | None:
        """Single sandbox file with contents, or ``None`` if absent."""
        ...

    async def set(
        self,
        org_id: str,
        upstream_id: str,
        name: str,
        contents: str,
        target_path: str,
        display_name: str | None = None,
    ) -> SandboxFileSummary:
        """Create or replace a sandbox file; return the post-write summary.

        ``display_name`` is the human-readable label shown in the
        dashboard listing. When ``None`` the implementation defaults
        it to ``name`` (for legacy / scripted callers that don't
        carry a display label).

        ``sha256`` and ``size_bytes`` are computed server-side and
        stored alongside the encrypted blob (cloud mode) or plaintext
        (standalone) so the listing path never has to decrypt.
        """
        ...

    async def delete(
        self, org_id: str, upstream_id: str, name: str
    ) -> None:
        """Remove a single sandbox file. Idempotent — missing → no-op."""
        ...

    async def delete_all(
        self, org_id: str, upstream_id: str
    ) -> None:
        """Cascade delete on upstream removal. Idempotent."""
        ...
