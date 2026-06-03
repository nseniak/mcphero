"""Tests for the :class:`SandboxFile` domain model + companions."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mcpolis.domain.model.sandbox_file import (
    MAX_FILE_BYTES,
    SandboxFile,
    SandboxFileSummary,
    compute_sha256,
    is_valid_sandbox_file_name,
    slugify_sandbox_file_name,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def make_file(
    *,
    name: str = "gcp-cred",
    display_name: str = "GCP service account",
    contents: str = "{}",
    target_path: str = "${HOME}/.config/gcloud/credentials.json",
) -> SandboxFile:
    return SandboxFile(
        name=name,
        display_name=display_name,
        contents=contents,
        target_path=target_path,
        sha256=compute_sha256(contents),
        size_bytes=len(contents.encode("utf-8")),
        created_at=_now(),
        updated_at=_now(),
    )


def test_round_trips_through_pydantic() -> None:
    f = make_file()
    dumped = f.model_dump()
    restored = SandboxFile.model_validate(dumped)
    assert restored == f


def test_summary_never_carries_contents() -> None:
    summary = SandboxFileSummary(
        name="x",
        display_name="X file",
        target_path="/x",
        sha256="a" * 64,
        size_bytes=1,
        created_at=_now(),
        updated_at=_now(),
    )
    # The model literally has no ``contents`` field, so a pydantic
    # extra-fields check would raise. Confirm the public attribute
    # surface stays clean.
    assert "contents" not in summary.model_dump()


def test_name_accepts_url_safe_slugs_and_legacy_uppercase() -> None:
    # New free-form-ish slug grammar — lowercase + dashes + dots.
    make_file(name="gcp-cred")
    make_file(name="kubeconfig.dev")
    # Legacy uppercase rows still validate (no migration cost on
    # existing data).
    make_file(name="GCP_CRED")


def test_name_rejects_spaces_and_disallowed_chars() -> None:
    with pytest.raises(Exception):
        make_file(name="has spaces")
    with pytest.raises(Exception):
        make_file(name="has/slashes")
    with pytest.raises(Exception):
        make_file(name="")


def test_display_name_must_not_be_empty() -> None:
    with pytest.raises(Exception):
        make_file(display_name="")
    with pytest.raises(Exception):
        make_file(display_name="   ")


def test_target_path_must_be_absolute_or_system_var() -> None:
    with pytest.raises(Exception):
        make_file(target_path="relative/path.json")
    # Absolute path is fine.
    make_file(target_path="/etc/foo.conf")
    # System variable prefix is fine.
    make_file(target_path="${HOME}/foo")


def test_target_path_must_not_be_empty() -> None:
    with pytest.raises(Exception):
        make_file(target_path="")


def test_compute_sha256_stable_for_same_contents() -> None:
    assert compute_sha256("hello") == compute_sha256("hello")
    assert compute_sha256("hello") != compute_sha256("HELLO")


def test_is_valid_sandbox_file_name() -> None:
    assert is_valid_sandbox_file_name("gcp-cred")
    assert is_valid_sandbox_file_name("GCP_CRED")
    assert is_valid_sandbox_file_name("kubeconfig.dev")
    assert is_valid_sandbox_file_name("a")
    assert not is_valid_sandbox_file_name("has spaces")
    assert not is_valid_sandbox_file_name("has/slashes")
    assert not is_valid_sandbox_file_name("")
    assert not is_valid_sandbox_file_name("a" * 65)


def test_slugify_sandbox_file_name_handles_common_inputs() -> None:
    # Filename-style input → lowercase, dotted segments preserved.
    assert slugify_sandbox_file_name("credentials.json") == "credentials.json"
    # Free-form display name with spaces and capitals.
    assert slugify_sandbox_file_name("GCP service account") == "gcp-service-account"
    # Garbage characters collapse to a single dash. The trailing
    # dash before ``.yaml`` is acceptable noise — still a valid
    # URL-safe slug, just slightly ugly. The operator can override
    # the auto-slug in the form if it matters.
    assert slugify_sandbox_file_name("foo  /  bar!!.yaml") == "foo-bar-.yaml"
    # All-disallowed input → empty string (caller must fall back).
    assert slugify_sandbox_file_name("@@@") == ""
    # Very long input → truncated, no trailing dash.
    long_input = "a" * 80 + "-tail"
    out = slugify_sandbox_file_name(long_input)
    assert len(out) <= 64
    assert not out.endswith("-")


def test_max_file_bytes_is_128_kib() -> None:
    # Pin the cap as a contract so a sneaky bump shows up in code
    # review. 128 KiB covers credential / config files with ~5×
    # headroom for the largest realistic case (CA bundles ~100 KB);
    # a binary upload that exceeds it is almost always a mistake.
    assert MAX_FILE_BYTES == 128 * 1024
