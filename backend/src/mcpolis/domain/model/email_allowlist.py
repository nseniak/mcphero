"""An email allow-list matched the way the app compares addresses.

Google, the only sign-in provider in cloud mode, reports an address in
its own spelling, not necessarily the one an operator typed into an env
var. So an entry ``Ops@Example.com`` must admit the sign-in
``ops@example.com``, and vice versa.

Membership uses ``email_key`` (ASCII letter case and surrounding ASCII
white space ignored, every other character exact), the same rule as
every other email comparison in the app. No substring or domain
matching.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator

from mcpolis.domain.model.email_address import email_key


class EmailAllowlist(Collection[str]):
    """Email addresses; ``email in allowlist`` compares ``email_key``
    values. Blank entries are dropped."""

    def __init__(self, emails: Iterable[str] = ()) -> None:
        self._keys = frozenset(
            key for key in (email_key(e) for e in emails) if key
        )

    def __contains__(self, email: object) -> bool:
        return isinstance(email, str) and email_key(email) in self._keys

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._keys))

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return f"EmailAllowlist({sorted(self._keys)!r})"
