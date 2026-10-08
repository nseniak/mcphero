"""Heuristic detection of raw credentials in upstream env / headers.

Defensive: callers run the scan on save and emit a structured
``secret_in_json_detected`` log event when something looks suspicious.
We never reject — the user may have a legitimate reason — and we never
log the value itself, only enough context for an admin to find the
problem (upstream id, field, key, which pattern matched, and a short
prefix preview of the offending value).

The patterns mirror the client-side scanner at
``frontend/src/lib/secret-detection.ts``. Keep the two in sync — drift
shows up as a value that fires only on one side.

The same patterns also hide credentials from a reader that must never
see one, such as the AI client behind the Admin MCP's ``get_upstream``
(``hide_secret_values``, ``hide_secrets_in_text``, ``hide_secret_args``).
Hiding errs the other way from the warning: an env var or header value
is hidden unless it is made only of Variable references (``${NAME}``),
which hold no secret, and in a URL or a command line whatever MAY be a
credential is hidden.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel

from mcpolis.domain.services.template_var_substitution import (
    has_placeholder,
    strip_placeholders,
)

ScanField = Literal["env", "headers"]


class ScanFinding(BaseModel):
    """One suspicious value found in env / headers."""

    field: ScanField
    key: str
    pattern: str
    # Short prefix so the user can recognise *which* value triggered
    # the warning without us echoing the full secret. Always 6 chars +
    # ellipsis when the value is longer than 6.
    match_preview: str


# Provider-specific token shapes. Anchored loosely (not ``^`` /``$``)
# so we still catch tokens embedded in larger strings (e.g. inside a
# ``Bearer ghp_...`` header value).
_PROVIDER_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("github_token", re.compile(r"\bgh[psoru]_[A-Za-z0-9]{16,}\b")),
    ("openai_or_stripe_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"
        ),
    ),
]

# Key-name heuristic: when the JSON key looks like a secret holder, we
# also apply the entropy fallback. Without this gate, the entropy
# check fires on hashes, opaque IDs, etc.
_SECRET_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(token|secret|key|password|auth|bearer|credential|api[_-]?key)"
)

# Entropy threshold for the heuristic fallback. 4.0 bits/char is a
# reasonable cut for "looks like a credential, not English text" —
# random base64/hex tokens hit ~5 bits/char; well-formed sentences sit
# around 3.5.
_MIN_ENTROPY_BITS: Final[float] = 4.0
_MIN_ENTROPY_LENGTH: Final[int] = 16


def _shannon_entropy(value: str) -> float:
    """Bits-per-character Shannon entropy."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _build_preview(value: str) -> str:
    """First 6 chars + ``…`` when longer; whole value otherwise."""
    if len(value) <= 6:
        return value
    return value[:6] + "…"


def _scan_value(field: ScanField, key: str, value: str) -> ScanFinding | None:
    """First pattern that matches, or ``None``.

    Order matters: provider regexes win over the entropy heuristic so
    the more-specific reason gets reported.
    """
    if has_placeholder(value):
        # Already a ``${VAR}`` reference — nothing to flag.
        return None
    for pattern_name, regex in _PROVIDER_PATTERNS:
        if regex.search(value):
            return ScanFinding(
                field=field, key=key, pattern=pattern_name,
                match_preview=_build_preview(value),
            )
    if (
        _SECRET_KEY_RE.search(key)
        and len(value) >= _MIN_ENTROPY_LENGTH
        and _shannon_entropy(value) >= _MIN_ENTROPY_BITS
    ):
        return ScanFinding(
            field=field, key=key, pattern="high_entropy",
            match_preview=_build_preview(value),
        )
    return None


def scan_for_secrets(
    *,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> list[ScanFinding]:
    """Walk env + headers, return one finding per suspect value."""
    findings: list[ScanFinding] = []
    for key, value in (env or {}).items():
        finding = _scan_value("env", key, value)
        if finding is not None:
            findings.append(finding)
    for key, value in (headers or {}).items():
        finding = _scan_value("headers", key, value)
        if finding is not None:
            findings.append(finding)
    return findings


# --- Hiding credentials from a reader ---
#
# For a reader that must never see a credential, such as the AI client
# behind the Admin MCP's ``get_upstream``. Each value an MCP's connection
# settings hold goes through one of three functions, by where it sits:
#
# - ``hide_secret_values``: env var and header values. Deny by default:
#   a value shows only when it is made of Variable references alone
#   (``${NAME}``, possibly after an auth scheme: ``Bearer ${TOKEN}``).
# - ``hide_secrets_in_text``: a URL, a command, one argument. The parts
#   that may hold a credential are hidden: a URL's user info (whatever is
#   before ``@``), an env var set on the command line (``NAME=value``,
#   deny by default like an env var's value), the value of a
#   credential-sounding ``name=value`` or JSON ``"name": "value"``, the
#   word after ``Bearer`` or ``Basic``, and anything shaped like a key.
# - ``hide_secret_args``: command-line arguments, each through
#   ``hide_secrets_in_text``, plus what only the argument list shows: the
#   value after a flag (``--api-key <value>``), the whole value of an
#   argument that sets one (``--token=...``, ``NAME=...``), and a header
#   passed as an argument (deny by default after ``--header``, like a
#   header's value).

# What a hidden value is replaced with.
HIDDEN_VALUE: Final[str] = "[hidden]"

# A value that opens with an auth scheme carries the credential after it
# (``Bearer <token>``, ``token <token>``).
_LEADING_AUTH_SCHEME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)^\s*(?:bearer|basic|token|bot)\s+",
)
# The same inside free text, for the two schemes that can't be prose.
_AUTH_SCHEME_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?i)(?P<head>\b(?:bearer|basic)\s+)(?P<value>[^\s"']+)""",
)
# ``scheme://userinfo@host``: a user name and a password, or a token
# alone. It runs to the last ``@`` before the path, so a password holding
# a raw ``@`` is hidden whole.
_URL_USERINFO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^\s/?#]*)@",
)
# An env var set on a command line, opening a word: ``env NAME=value``,
# ``docker run -e NAME=value``.
_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<head>(?:^|(?<=\s))(?P<name>[A-Z_][A-Z0-9_]*)=)(?P<value>\S*)",
)
# The name of an env var, by convention.
_ENV_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z_][A-Z0-9_]*")
# One whole argument that sets a value: ``--name=value``, ``NAME=value``.
# The value runs to the end of the argument, spaces included.
_ARG_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<head>(?P<dashes>--?)?(?P<name>[A-Za-z0-9_.\-]+)=)(?P<value>.*)$",
    re.DOTALL,
)
# ``?name=value`` / ``&name=value`` in a URL, ``--name=value`` in a
# command line.
_NAMED_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<head>(?:[?&]|(?:^|\s)--?)(?P<name>[A-Za-z0-9_.\-]+)=)(?P<value>[^\s&#]*)",
)
# ``"name": "value"`` in JSON passed on a command line
# (``--config '{"apiKey": "..."}'``).
_JSON_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r'(?P<head>"(?P<name>[A-Za-z0-9_.\-]+)"\s*:\s*")(?P<value>(?:[^"\\]|\\.)*)"',
)
# A command-line flag standing alone (``--api-key``): its value is the
# next argument.
_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"^--?(?P<name>[A-Za-z0-9_.\-]+)$")
# The flag whose value is a header (``mcp-remote --header``, ``curl -H``).
_HEADER_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"^(?:-H|--?headers?)$")
# A header passed as one argument (``Authorization: Bearer ...``), but
# not a URL (``https://...``).
_HEADER_ARG_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9\-]*)\s*:(?!//)\s*(?P<value>.*)$",
)
# A name whose value may be a credential: the scanner's words, plus some
# a save-time warning would find too noisy.
_CREDENTIAL_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(token|secret|key|pass|pwd|auth|bearer|credential|cookie|session"
    r"|signature|(?<![a-z])(?:sig|pat)(?![a-z]))",
)
# A run of the characters a generated key is made of: letters, digits,
# ``-``, ``_`` and ``+``. Dots, slashes and ``=`` end it, so a host name,
# a path or a ``name==1.2.3`` version is several runs.
_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_+\-]+")
# A run this long, in which letters and digits take turns this often,
# is shaped like a generated key (a hex or base64 token, a UUID); a
# package name such as ``mcp-neo4j-cypher`` is not.
_KEY_MIN_LENGTH: Final[int] = 16
_KEY_MIN_LETTER_DIGIT_TURNS: Final[int] = 3


def _looks_like_a_key(run: str) -> bool:
    if len(run) < _KEY_MIN_LENGTH:
        return False
    turns = sum(
        1
        for a, b in itertools.pairwise(run)
        if (a.isdigit() and b.isalpha()) or (a.isalpha() and b.isdigit())
    )
    return turns >= _KEY_MIN_LETTER_DIGIT_TURNS


def _is_a_reference_name(match: re.Match[str]) -> bool:
    """Whether the run ``match`` is the NAME of a ``${NAME}``: a Variable
    reference is shown, whatever its name looks like."""
    text = match.string
    return (
        text[max(match.start() - 2, 0):match.start()] == "${"
        and text[match.end():match.end() + 1] == "}"
    )


def _hidden_run(match: re.Match[str]) -> str:
    if _looks_like_a_key(match[0]) and not _is_a_reference_name(match):
        return HIDDEN_VALUE
    return match[0]


def _hide_keys(text: str) -> str:
    """``text`` with every token of a known provider shape and every
    key-shaped run replaced by ``HIDDEN_VALUE``."""
    for _, regex in _PROVIDER_PATTERNS:
        text = regex.sub(HIDDEN_VALUE, text)
    return _RUN_RE.sub(_hidden_run, text)


def _only_references(value: str) -> bool:
    """Whether ``value`` holds nothing typed in clear but Variable
    references (``${NAME}``), possibly after an auth scheme
    (``Bearer ${TOKEN}``) and white space. An empty value holds nothing."""
    return not _LEADING_AUTH_SCHEME_RE.sub("", strip_placeholders(value)).strip()


def may_carry_secret(name: str, value: str) -> bool:
    """Whether ``value``, named ``name`` (a query parameter, a
    command-line flag, a JSON member, a header passed as an argument),
    may hold a credential typed in clear: a credential-sounding name, an
    auth scheme, or something shaped like a key. A value made only of
    Variable references holds none."""
    if _only_references(value):
        return False
    if _CREDENTIAL_NAME_RE.search(name) or _LEADING_AUTH_SCHEME_RE.match(
        strip_placeholders(value),
    ):
        return True
    return _hide_keys(value) != value


def _hides_assigned_value(name: str, value: str) -> bool:
    """Whether the value of ``name=value`` on a command line is hidden:
    an env var's (an upper-case ``NAME``) unless it is only references,
    like an env var's value; any other's when it may be a credential."""
    if _ENV_NAME_RE.fullmatch(name):
        return not _only_references(value)
    return may_carry_secret(name, value)


def hide_secret_values(values: Mapping[str, str]) -> dict[str, str]:
    """``values`` (env vars or headers) with every value replaced by
    ``HIDDEN_VALUE`` but those made only of Variable references. The
    names are kept.

    Deny by default: a value whose name and shape look harmless can still
    be a credential (``DATABASE_URL`` holding a password, a ``Cookie``,
    a token under the name ``PAT``), and no list of names and shapes
    finds them all."""
    return {
        name: value if _only_references(value) else HIDDEN_VALUE
        for name, value in values.items()
    }


def hide_secrets_in_text(text: str) -> str:
    """``text`` (a URL, a command, one argument) with what may be a
    credential replaced by ``HIDDEN_VALUE``: a URL's user info, the value
    of an env var set on a command line, the value of a
    credential-sounding ``name=value`` or JSON ``"name": "value"``, the
    word after ``Bearer`` or ``Basic``, and anything shaped like a key (in
    a URL's path, a query value, an argument). Variable references are
    shown."""

    def userinfo(match: re.Match[str]) -> str:
        if _only_references(match["userinfo"].replace(":", "")):
            return match[0]
        return f"{match['head']}{HIDDEN_VALUE}@"

    def env_assignment(match: re.Match[str]) -> str:
        if not _hides_assigned_value(match["name"], match["value"]):
            return match[0]
        return f"{match['head']}{HIDDEN_VALUE}"

    def named_value(match: re.Match[str]) -> str:
        if not may_carry_secret(match["name"], match["value"]):
            return match[0]
        return f"{match['head']}{HIDDEN_VALUE}"

    def json_member(match: re.Match[str]) -> str:
        if not may_carry_secret(match["name"], match["value"]):
            return match[0]
        return f'{match["head"]}{HIDDEN_VALUE}"'

    def scheme_value(match: re.Match[str]) -> str:
        if _only_references(match["value"]):
            return match[0]
        return f"{match['head']}{HIDDEN_VALUE}"

    text = _URL_USERINFO_RE.sub(userinfo, text)
    text = _ENV_ASSIGNMENT_RE.sub(env_assignment, text)
    text = _NAMED_VALUE_RE.sub(named_value, text)
    text = _JSON_MEMBER_RE.sub(json_member, text)
    text = _AUTH_SCHEME_VALUE_RE.sub(scheme_value, text)
    return _hide_keys(text)


# A known password shorter than this is left in an error text: replacing
# a two-letter value would cut every word that holds those letters.
_KNOWN_SECRET_MIN_LENGTH: Final[int] = 4
# The characters a URL encoder may leave as they are: none, a path's
# ``/``, httpx's ``/:@`` in a query, and every sub-delimiter httpx keeps
# in a path.
_URL_SAFE_SETS: Final[tuple[str, ...]] = ("", "/", "/:@", "!$&'()*+,;=:@/")


def _secret_forms(secret: str) -> set[str]:
    """The ways ``secret`` may be spelled inside an error text: as typed,
    URL-encoded by each common encoder, and escaped by ``repr`` (an
    ``OSError`` quoting a path) or by JSON."""
    forms = {
        secret,
        urllib.parse.quote_plus(secret),
        repr(secret)[1:-1],
        json.dumps(secret)[1:-1],
    }
    forms.update(
        urllib.parse.quote(secret, safe=safe) for safe in _URL_SAFE_SETS
    )
    return forms


def hide_secrets_in_error(text: str, known_secrets: Iterable[str]) -> str:
    """``text``, an error an MCP's connect or tool discovery raised, with
    every one of ``known_secrets`` (that MCP's password Variables, as
    substituted into its URL, headers or command) replaced by
    ``HIDDEN_VALUE`` in any spelling ``_secret_forms`` lists, ignoring
    letter case (a URL's host name is lower-cased), then what
    ``hide_secrets_in_text`` finds.

    Such an error often quotes the URL or command it failed on, with
    the Variables filled in (``Client error '401' for url '...'``), and
    it is saved and shown to admins: on the MCP's page, in the audit
    log, in a Connect or Start answer."""
    forms = {
        form
        for secret in known_secrets
        for form in _secret_forms(secret)
        if len(form) >= _KNOWN_SECRET_MIN_LENGTH
    }
    if forms:
        # Longest first, so a password that contains another is hidden
        # whole.
        pattern = re.compile(
            "|".join(
                re.escape(form) for form in sorted(forms, key=len, reverse=True)
            ),
            re.IGNORECASE,
        )
        text = pattern.sub(HIDDEN_VALUE, text)
    return hide_secrets_in_text(text)


def _hide_header_arg(text: str) -> str | None:
    """``text``, a header passed as an argument (``Name: value``), with
    its value hidden like those of the headers: unless it is only
    references. None when ``text`` is no header."""
    header = _HEADER_ARG_RE.match(text)
    if header is None:
        return None
    if _only_references(header["value"]):
        return text
    return f"{header['name']}: {HIDDEN_VALUE}"


def _hide_arg(arg: str, previous: str) -> str:
    """One command-line argument as ``hide_secret_args`` shows it;
    ``previous`` is the argument before it."""
    if _HEADER_FLAG_RE.match(previous) is not None:
        header_arg = _hide_header_arg(arg)
        if header_arg is not None:
            return header_arg
    assignment = _ARG_ASSIGNMENT_RE.match(arg)
    if assignment is not None:
        name, value = assignment["name"], assignment["value"]
        flag_name = f"{assignment['dashes'] or ''}{name}"
        header_arg = (
            _hide_header_arg(value) if _HEADER_FLAG_RE.match(flag_name) else None
        )
        if header_arg is not None:
            return f"{assignment['head']}{header_arg}"
        if (
            may_carry_secret(name, value) if assignment["dashes"]
            else _hides_assigned_value(name, value)
        ):
            return f"{assignment['head']}{HIDDEN_VALUE}"
    flag = _FLAG_RE.match(previous)
    if (
        flag is not None
        and not arg.startswith("-")
        and may_carry_secret(flag["name"], arg)
    ):
        return HIDDEN_VALUE
    header = _HEADER_ARG_RE.match(arg)
    if header is not None and may_carry_secret(header["name"], header["value"]):
        return f"{header['name']}: {HIDDEN_VALUE}"
    return hide_secrets_in_text(arg)


def hide_secret_args(args: Sequence[str]) -> list[str]:
    """Command-line ``args`` with what may be a credential hidden: the
    value after a credential-sounding flag (``--api-key <value>``) or a
    key-shaped one, the whole value of an argument that sets one
    (``--token=...``, ``NAME=...`` like an env var's value), a header
    passed as an argument (after ``--header`` or ``-H``, every value but
    Variable references, like a header's value), and what
    ``hide_secrets_in_text`` finds in each."""
    return [
        _hide_arg(arg, previous)
        for previous, arg in itertools.pairwise(["", *args])
    ]
