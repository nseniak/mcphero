"""The operator email list ignores letter case, and nothing else.

Google, the only cloud sign-in provider, treats email addresses as
case-insensitive, and it does not necessarily spell an address the way
an operator typed it into ``MCPOLIS_SUPERADMIN_EMAILS``. Every operator
check (dashboard, operator API, operator MCP, debug pages, org
drill-down) asks ``email in <this list>``, so the list itself carries
the rule.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.model.email_allowlist import EmailAllowlist


def make_allowlist() -> EmailAllowlist:
    # Written the way an operator writes the env var: capitals, spaces,
    # a trailing comma.
    return EmailAllowlist(" Ops@Example.com , second-ops@example.com ,".split(","))


@pytest.mark.parametrize(
    "signed_in",
    ["ops@example.com", "OPS@EXAMPLE.COM", "Ops@Example.com", " ops@example.com "],
    ids=["lower", "upper", "as-listed", "spaces"],
)
def test_listed_email_matches_whatever_its_letter_case(signed_in: str) -> None:
    """An operator listed as ``Ops@Example.com`` is recognised however the
    sign-in spells the same address."""
    assert signed_in in make_allowlist()


@pytest.mark.parametrize(
    "signed_in",
    ["evil-ops@example.com", "ops@example.com.attacker.example", "ops@example", ""],
    ids=["contains", "extends", "shorter", "empty"],
)
def test_email_that_only_resembles_a_listed_email_does_not_match(
    signed_in: str,
) -> None:
    """Apart from letter case and spaces, matching stays exact."""
    assert signed_in not in make_allowlist()


def test_blank_entries_are_dropped_and_case_variants_count_once() -> None:
    """The count the startup log reports is the number of distinct
    operators: blanks are ignored and two spellings of one address are
    one operator."""
    allowlist = EmailAllowlist(["ops@example.com", " OPS@example.com", "", "  "])
    assert len(allowlist) == 1
    assert list(allowlist) == ["ops@example.com"]
    assert not EmailAllowlist([""])


@pytest.mark.parametrize(
    ("listed", "signed_in"),
    [
        ("straße@example.com", "strasse@example.com"),
        ("sam@example.com", "\u017fam@example.com"),
        ("kate@example.com", "\u212aate@example.com"),
    ],
    ids=["sharp-s", "long-s", "kelvin-sign"],
)
def test_non_ascii_letters_are_not_merged_with_ascii_ones(
    listed: str, signed_in: str,
) -> None:
    """Only ASCII letter case is ignored, the rule of every email
    comparison in the app (``email_key``): ``straße`` is not ``strasse``,
    and a long s or a Kelvin sign does not pass for ``s`` or ``k``."""
    assert signed_in not in EmailAllowlist([listed])
    assert listed not in EmailAllowlist([signed_in])


def test_only_text_can_match() -> None:
    assert None not in make_allowlist()
