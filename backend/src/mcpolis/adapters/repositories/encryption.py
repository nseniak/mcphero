"""AES-256-GCM encryption helpers for the cloud-mode Mongo backend.

Cloud mode stores OAuth tokens, client secrets, and similar credential
material in Mongo. Those fields are encrypted at rest with AES-256-GCM
using a key derived from ``MCPOLIS_ENCRYPTION_KEY`` via HKDF.

Usage:
    fernet = FieldEncryptor.from_settings_secret(encryption_key)
    blob = fernet.encrypt_string("access_token_xyz")
    plain = fernet.decrypt_string(blob)

Only the Mongo repositories call into this module. Domain code never
sees ciphertext or keys. Standalone mode does not use this module — the
file repositories write plaintext JSON on the user's own machine, same
as today.
"""
from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Sentinel prefix so we can tell a plaintext value from an encrypted
# blob at a glance. ``enc:v1:`` marks a base64-encoded (nonce || ciphertext)
# produced by this module with the v1 key-derivation parameters.
_PREFIX = "enc:v1:"
_NONCE_LEN = 12  # AES-GCM spec-recommended nonce length


def derive_encryption_key(master_secret: str) -> bytes:
    """HKDF-derive a 32-byte AES key from ``MCPOLIS_ENCRYPTION_KEY``.

    ``info`` matches the pattern used by ``dashboard_auth.get_signing_key``
    so the same master secret can produce distinct keys for different
    purposes without reuse.
    """
    # ``info`` is a stable key-derivation salt, not a brand name.
    # Rotating it invalidates every ciphertext already written under the
    # old label, so do NOT change this string on a whim — treat it like a
    # schema version. The ``-v1`` suffix is the real axis for bumping
    # (e.g. for future per-org keying noted in the security audit).
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=None,
        info=b"mcpolis-encryption-v1",
    ).derive(master_secret.encode("utf-8"))


class FieldEncryptor:
    """Encrypts/decrypts individual field values with AES-256-GCM.

    The class is stateless beyond the derived key: every ``encrypt``
    call generates a fresh random nonce, so two encryptions of the same
    plaintext produce different ciphertexts. This is important for the
    architectural tests that grep raw Mongo docs for plaintext tokens.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(
                f"FieldEncryptor requires a 32-byte key, got {len(key)}"
            )
        self._aes = AESGCM(key)

    @classmethod
    def from_master_secret(cls, master_secret: str) -> FieldEncryptor:
        if not master_secret:
            raise ValueError(
                "FieldEncryptor requires a non-empty master secret "
                "(MCPOLIS_ENCRYPTION_KEY)"
            )
        return cls(derive_encryption_key(master_secret))

    def encrypt_string(self, plaintext: str) -> str:
        """Encrypt a string, returning a ``enc:v1:<base64>`` blob."""
        nonce = os.urandom(_NONCE_LEN)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt_string(self, blob: str) -> str:
        if not blob.startswith(_PREFIX):
            # Back-compat safety net: if an operator migrates data in by
            # hand we want a clear error, not a silent failure.
            raise ValueError(
                "Encrypted blob missing expected prefix — "
                "refusing to decrypt"
            )
        raw = base64.urlsafe_b64decode(blob[len(_PREFIX):].encode("ascii"))
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return self._aes.decrypt(nonce, ct, None).decode("utf-8")

    def encrypt_optional(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        return self.encrypt_string(plaintext)

    def decrypt_optional(self, blob: str | None) -> str | None:
        if blob is None:
            return None
        return self.decrypt_string(blob)


def encrypt_fields(
    doc: dict[str, Any], field_paths: list[str], encryptor: FieldEncryptor
) -> dict[str, Any]:
    """Return a shallow copy of ``doc`` with the named fields encrypted.

    ``field_paths`` supports dotted notation (``"user_tokens.access_token"``).
    Missing paths are silently skipped. Only string values are encrypted
    — ``None`` and other types pass through. The result is a new dict;
    the input is never mutated.
    """
    # Deep-copy only the branches we touch so we don't mutate the input.
    result: dict[str, Any] = dict(doc)
    for path in field_paths:
        _set_field(result, path.split("."), encryptor.encrypt_string, skip_none=True)
    return result


def decrypt_fields(
    doc: dict[str, Any], field_paths: list[str], encryptor: FieldEncryptor
) -> dict[str, Any]:
    result: dict[str, Any] = dict(doc)
    for path in field_paths:
        _set_field(result, path.split("."), encryptor.decrypt_string, skip_none=True)
    return result


def _set_field(
    container: dict[str, Any],
    parts: list[str],
    transform: Any,
    *,
    skip_none: bool,
) -> None:
    if not parts:
        return
    key = parts[0]
    if key not in container:
        return
    value = container[key]
    if len(parts) == 1:
        if value is None and skip_none:
            return
        if isinstance(value, str):
            container[key] = transform(value)
        return
    if isinstance(value, dict):
        nested: dict[str, Any] = dict(value)  # pyright: ignore[reportUnknownArgumentType]
        _set_field(nested, parts[1:], transform, skip_none=skip_none)
        container[key] = nested
