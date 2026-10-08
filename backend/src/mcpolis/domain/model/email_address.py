"""Comparing email addresses.

An address reaches the app in more than one spelling: an admin types
``Bob@Acme.com`` into the invite form, Google reports ``bob@acme.com``
at sign-in. The letter case of an address carries no meaning, so code
that asks "is this the same person?" compares ``email_key`` values.

Only ASCII letter case is ignored: ``email_key`` turns ``A``-``Z`` into
``a``-``z`` and changes no other character. Unicode case folding
(``str.casefold``) also maps distinct characters onto ASCII ones (``ß``
to ``ss``, the long s U+017F to ``s``, the Kelvin sign U+212A to
``k``), which made two different mailboxes one person: an invitation
sent to ``strasse@`` was accepted by ``straße@``, with the role it
carried. The dashboard's ``sameEmail``
(``frontend/src/lib/email-address.ts``) applies the same rule, and the
shared cases in ``frontend/src/lib/email-address-cases.json`` hold both
sides to it.

Comparison only: addresses are still stored as they were given. So an
org's users can hold ``Bob@Acme.com`` while Bob signs in, holds his
sign-ins and accepts the invitation as ``bob@acme.com``: code that finds
a person in a stored collection uses ``find_address``, and code that
acts on the person's own data uses the address they signed in with. An
org's users saved before letter case was ignored may hold two spellings
of one person: removing a person or changing their role acts on
``every_spelling``.
"""
from __future__ import annotations

import string
from collections.abc import Collection, Iterable
from typing import Final

# What ``email_key`` trims from both ends: ASCII white space only, the
# same set as the frontend's rule.
_ASCII_WHITESPACE: Final[str] = " \t\n\r\x0b\x0c"
_ASCII_LOWER_CASE: Final[dict[int, int]] = str.maketrans(
    string.ascii_uppercase, string.ascii_lowercase,
)


def email_key(email: str) -> str:
    """The form of ``email`` two addresses are compared in: without
    surrounding ASCII white space, ``A``-``Z`` lower-cased, every other
    character as it is."""
    return email.strip(_ASCII_WHITESPACE).translate(_ASCII_LOWER_CASE)


def same_email(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` name the same address."""
    return email_key(a) == email_key(b)


def every_spelling(addresses: Iterable[str], email: str) -> list[str]:
    """Every address of ``addresses`` equal to ``email`` ignoring letter
    case, in their order. Usually one; two when an org's users saved
    before letter case was ignored hold two spellings of one person."""
    key = email_key(email)
    return [address for address in addresses if email_key(address) == key]


def find_address(addresses: Collection[str], email: str) -> str | None:
    """How ``addresses`` spell ``email``: ``email`` itself when it is
    there as given, else the first address equal to it ignoring letter
    case. None when there is neither.

    ``addresses`` is typically the keys of an org's users, so the answer
    is the key to read or change that person's entry with."""
    if email in addresses:
        return email
    spellings = every_spelling(addresses, email)
    return spellings[0] if spellings else None
