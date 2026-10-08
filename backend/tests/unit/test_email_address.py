"""Only ASCII letter case and ASCII white space at the ends carry no
meaning in an address.

Unicode case folding (``str.casefold``, which the comparison used before)
maps distinct characters onto ASCII ones: ``straße@`` compared equal to
``strasse@``, ``ſam@`` (long s) to ``sam@``, ``Kate@`` written with the
Kelvin sign to ``kate@``. Two mailboxes then counted as one person, and
an invitation sent to one was accepted by the other, with its role.

The cases come from ``frontend/src/lib/email-address-cases.json``, which
the dashboard's ``sameEmail`` test runs too: the two sides must agree on
every pair.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from mcpolis.domain.model.email_address import (
    email_key,
    every_spelling,
    find_address,
    same_email,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "email-address-cases.json"


class EmailCases(BaseModel):
    """Pairs that name one person, and pairs that name two."""

    same: list[tuple[str, str]]
    different: list[tuple[str, str]]


def load_email_cases() -> EmailCases:
    return EmailCases.model_validate_json(CASES_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("pair", load_email_cases().same, ids=repr)
def test_the_shared_cases_of_one_person(pair: tuple[str, str]) -> None:
    assert same_email(*pair)


@pytest.mark.parametrize("pair", load_email_cases().different, ids=repr)
def test_the_shared_cases_of_two_people(pair: tuple[str, str]) -> None:
    assert not same_email(*pair)


def test_only_ascii_letters_change_case() -> None:
    assert email_key(" \tBOB.Smith@Acme.COM\n") == "bob.smith@acme.com"
    assert not same_email("strasse@example.de", "straße@example.de")
    assert not same_email("sam@example.com", "ſam@example.com")
    assert not same_email("kate@example.com", "Kate@example.com")


def test_every_spelling_of_an_address_is_found() -> None:
    users = ["bob@x.com", "alice@x.com", "Bob@X.com"]

    assert every_spelling(users, "BOB@x.com") == ["bob@x.com", "Bob@X.com"]
    assert every_spelling(users, "carol@x.com") == []
    # The spelling as given wins; else the first one stored.
    assert find_address(users, "Bob@X.com") == "Bob@X.com"
    assert find_address(users, "BOB@X.COM") == "bob@x.com"
    assert find_address(users, "carol@x.com") is None
