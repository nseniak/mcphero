"""Tests for the ``${NAME}`` substitution helper.

The helper is pure — these tests exercise it directly, no I/O.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.model.template_var import MissingTemplateVarError
from mcpolis.domain.services.template_var_substitution import (
    find_placeholders,
    has_placeholder,
    substitute_mapping,
    substitute_sequence,
    substitute_string,
)


def _make_resolver(values: dict[str, str]) -> "object":
    def resolver(name: str) -> str | None:
        return values.get(name)
    return resolver


def test_substitute_string_replaces_single_placeholder() -> None:
    resolver = _make_resolver({"GITHUB_TOKEN": "ghp_abc"})
    assert substitute_string(
        "Bearer ${GITHUB_TOKEN}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="github",
    ) == "Bearer ghp_abc"


def test_substitute_string_handles_multiple_placeholders() -> None:
    resolver = _make_resolver({"A": "1", "B": "2"})
    assert substitute_string(
        "${A}-${B}-${A}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "1-2-1"


def test_substitute_string_unresolved_raises_missing_secret_error() -> None:
    resolver = _make_resolver({})
    with pytest.raises(MissingTemplateVarError) as exc:
        substitute_string(
            "${MISSING}",
            resolver=resolver,  # type: ignore[arg-type]
            upstream_id="github",
        )
    assert exc.value.name == "MISSING"
    assert exc.value.upstream_id == "github"


def test_substitute_string_passes_through_literal_dollar() -> None:
    resolver = _make_resolver({})
    # ``$NAME`` (no braces) is intentionally NOT recognised.
    assert substitute_string(
        "price=$10 not a placeholder",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "price=$10 not a placeholder"


def test_substitute_string_ignores_invalid_placeholder_names() -> None:
    resolver = _make_resolver({})
    # Lowercase and spaces don't match the regex; the literal text
    # passes through and the user notices their broken value instead
    # of getting a confusing "missing secret" error.
    assert substitute_string(
        "${lowercase} and ${spaces are bad}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "${lowercase} and ${spaces are bad}"


def test_substitute_string_does_not_recurse_on_resolved_value() -> None:
    # If the resolver returns a value that itself contains ``${X}``,
    # we DO NOT re-expand. Prevents accidental recursion / loops.
    resolver = _make_resolver({"A": "${B}", "B": "should-not-be-reached"})
    assert substitute_string(
        "${A}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "${B}"


def test_substitute_mapping_walks_every_value() -> None:
    resolver = _make_resolver({"T": "tok"})
    out = substitute_mapping(
        {"AUTH": "Bearer ${T}", "X_OTHER": "static"},
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    )
    assert out == {"AUTH": "Bearer tok", "X_OTHER": "static"}


def test_substitute_mapping_does_not_mutate_input() -> None:
    inputs = {"A": "${T}"}
    resolver = _make_resolver({"T": "tok"})
    _ = substitute_mapping(
        inputs, resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    )
    assert inputs == {"A": "${T}"}


def test_has_placeholder_detects_one() -> None:
    assert has_placeholder("Bearer ${T}") is True
    assert has_placeholder("plain") is False


def test_find_placeholders_returns_unique_ordered() -> None:
    assert find_placeholders("${A}${B}${A}") == ["A", "B"]


# --- Backslash escape: ``\${NAME}`` round-trips literally ---


def test_substitute_string_escape_passes_through_literal_token() -> None:
    """``\\${NAME}`` is a literal ``${NAME}`` after substitution.

    Used when a downstream tool's own syntax (e.g. an inline Python
    snippet that genuinely references a host env var) collides with
    our placeholder grammar."""
    resolver = _make_resolver({"NAME": "should-not-be-used"})
    assert substitute_string(
        r"\${NAME}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "${NAME}"


def test_substitute_string_escape_does_not_consume_resolver() -> None:
    """An escaped placeholder must NOT trigger a missing-var error
    even when the resolver doesn't carry the name."""
    resolver = _make_resolver({})
    assert substitute_string(
        r"echo \${UNRELATED}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "echo ${UNRELATED}"


def test_substitute_string_mixes_escaped_and_unescaped() -> None:
    """An escape on one occurrence does not prevent expansion of a
    different unescaped token in the same string."""
    resolver = _make_resolver({"REAL": "value"})
    assert substitute_string(
        r"${REAL}-\${LITERAL}",
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == "value-${LITERAL}"


def test_has_placeholder_ignores_escapes() -> None:
    """An escaped-only string carries no live placeholder."""
    assert has_placeholder(r"\${NAME}") is False
    assert has_placeholder(r"plain \${X} more") is False
    assert has_placeholder(r"plain ${X}") is True


def test_find_placeholders_skips_escaped() -> None:
    """Escaped tokens must not be returned — there is nothing to
    resolve, and the resolver shouldn't be asked about them."""
    assert find_placeholders(r"\${A}${B}") == ["B"]


# --- substitute_sequence ---


def test_substitute_sequence_walks_every_element() -> None:
    resolver = _make_resolver({"T": "tok"})
    assert substitute_sequence(
        ["-c", "print(${T})"],
        resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    ) == ["-c", "print(tok)"]


def test_substitute_sequence_does_not_mutate_input() -> None:
    inputs = ["${T}"]
    resolver = _make_resolver({"T": "tok"})
    _ = substitute_sequence(
        inputs, resolver=resolver,  # type: ignore[arg-type]
        upstream_id="x",
    )
    assert inputs == ["${T}"]
