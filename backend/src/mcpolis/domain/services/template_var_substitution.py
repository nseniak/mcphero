"""``${NAME}`` substitution for every user-controlled string field on
an :class:`~mcpolis.domain.model.upstream.UpstreamDefinition`.

Pure helpers — no I/O, no logging. Resolution is delegated to a
``Resolver`` callable supplied by the caller (typically a closure over
a :class:`~mcpolis.domain.ports.template_var_repository.TemplateVarRepository`
plus an ``(org_id, upstream_id)`` pair).

Substitution doesn't care whether the resolved value is a secret or a
plain config value — only the storage / display contract differs.

Substitution runs at session/request start, NOT at config load, so
plaintext secret values exist in memory only during the launch
handshake. Do NOT log the substituted dicts; ``test_no_env_logging``
enforces that statically.

A literal ``${NAME}`` token can be passed through verbatim by
escaping the dollar sign: ``\\${NAME}``. The backslash is consumed
and the rest is emitted as-is. No nested escaping rules — one
backslash, one escape, no resolver call.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from mcpolis.domain.model.template_var import MissingTemplateVarError

# Same name regex as :mod:`env_var` — uppercase env-var convention.
# Anchored inside ``${...}`` so we never match shell-style ``$NAME``
# (intentional; the documented syntax is brace-only). The optional
# leading backslash captures the escape: when group(1) is ``"\"``
# the match is treated as a literal pass-through.
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"(\\?)\$\{([A-Z_][A-Z0-9_]*)\}"
)

Resolver = Callable[[str], str | None]


def has_placeholder(value: str) -> bool:
    """``True`` if ``value`` contains at least one unescaped ``${NAME}``."""
    for match in _PLACEHOLDER_RE.finditer(value):
        if match.group(1) != "\\":
            return True
    return False


def find_placeholders(value: str) -> list[str]:
    """All ``NAME`` tokens referenced inside ``value`` (deduped, ordered).

    Escaped ``\\${NAME}`` matches are skipped — they round-trip
    literally and don't need to be resolved.
    """
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(value):
        if match.group(1) == "\\":
            continue
        seen[match.group(2)] = None
    return list(seen.keys())


def substitute_string(
    value: str,
    *,
    resolver: Resolver,
    upstream_id: str,
) -> str:
    """Replace every unescaped ``${NAME}`` in ``value`` with
    ``resolver(NAME)``.

    Raises :class:`MissingTemplateVarError` on the first unresolved name —
    fail-closed semantics. Literal ``$`` outside of ``${NAME}`` syntax
    passes through unchanged. ``${invalid name}`` (mixed case, spaces)
    does not match the regex, so it passes through unchanged too — the
    user gets an obviously-broken value instead of a confusing
    "missing env var" error.

    ``\\${NAME}`` is treated as an escape: the backslash is consumed
    and the rest is emitted literally as ``${NAME}``.
    """

    def _replace(match: re.Match[str]) -> str:
        if match.group(1) == "\\":
            # Escape: drop the leading backslash, emit ``${NAME}``.
            return match.group(0)[1:]
        name = match.group(2)
        resolved = resolver(name)
        if resolved is None:
            raise MissingTemplateVarError(upstream_id=upstream_id, name=name)
        return resolved

    return _PLACEHOLDER_RE.sub(_replace, value)


def substitute_mapping(
    values: dict[str, str],
    *,
    resolver: Resolver,
    upstream_id: str,
) -> dict[str, str]:
    """Apply :func:`substitute_string` to every value in ``values``.

    Returns a new dict; the input is not mutated. Used for the stdio
    ``env`` dict and the HTTP ``headers`` dict — the logic is
    identical.
    """
    # do not log the result — see test_no_env_logging
    return {
        key: substitute_string(
            val, resolver=resolver, upstream_id=upstream_id,
        )
        for key, val in values.items()
    }


def substitute_sequence(
    values: list[str],
    *,
    resolver: Resolver,
    upstream_id: str,
) -> list[str]:
    """Apply :func:`substitute_string` to every element in ``values``.

    Returns a new list; the input is not mutated. Used for stdio
    ``args`` so a ``${NAME}`` reference inside e.g. ``--api-key=${TOKEN}``
    expands the same way an ``env`` value does.
    """
    return [
        substitute_string(val, resolver=resolver, upstream_id=upstream_id)
        for val in values
    ]


def make_dict_resolver(values: dict[str, str]) -> Resolver:
    """Build a sync resolver from a name→value dict."""

    def _resolve(name: str) -> str | None:
        return values.get(name)

    return _resolve


def make_layered_resolver(*layers: dict[str, str]) -> Resolver:
    """Build a resolver that consults each layer left-to-right.

    Two layers in the live wiring: system Variables (``${HOME}``,
    future entries) shadow user Variables. The write-side rejects
    user-var names that would shadow a system name, but explicit
    precedence here keeps the contract independent of write-time
    enforcement.
    """

    def _resolve(name: str) -> str | None:
        for layer in layers:
            if name in layer:
                return layer[name]
        return None

    return _resolve
