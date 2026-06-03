"""Defense-in-depth: no raw motor collection access outside the wrapper.

Every Mongo read / write in the adapter layer must go through
``OrgScopedCollection``, which enforces ``org_id`` injection and
transparent field encryption. This test greps the
``adapters/repositories/mongo_*.py`` files for bare ``.find(``,
``.find_one(``, ``.insert_one(``, ``.update_one(``, ``.delete_one(``,
``.replace_one(``, ``.aggregate(``, ``.insert_many(``,
``.delete_many(``, or ``.count_documents(`` calls on anything other
than an ``OrgScopedCollection`` and fails the build if it finds one.

Two files are allow-listed:
- ``mongo_client.py`` — hosts ``OrgScopedCollection`` itself, so
  the raw motor calls there are by design.
- ``mongo_organization_repository.py`` — organizations are the root
  of the tenant tree and are NOT org-scoped; the collection IS the
  org index. It's the only repo that talks directly to motor.
"""
from __future__ import annotations

import re
from pathlib import Path

ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "mongo_client.py",
        "mongo_organization_repository.py",
    }
)

# Methods that would bypass ``OrgScopedCollection`` if called on a raw
# motor collection. We look for them as ``.<method>(`` tokens.
#
# ``find`` is intentionally omitted because Python strings, lists, and
# many other types also have a ``.find(`` method — the false-positive
# rate is unacceptable. Any code path that needs Mongo iteration must
# go through ``OrgScopedCollection.find_many`` or ``find_one``, both of
# which ARE on this list.
FORBIDDEN_METHODS: tuple[str, ...] = (
    "find_one",
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "delete_one",
    "delete_many",
    "aggregate",
    "count_documents",
    "distinct",
)

_PATTERN = re.compile(
    r"\.(?P<method>"
    + "|".join(FORBIDDEN_METHODS)
    + r")\s*\("
)


def _adapter_dir() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent / "src" / "mcpolis" / "adapters" / "repositories"


def test_no_raw_collection_access_in_mongo_adapters() -> None:
    repo_dir = _adapter_dir()
    offenders: list[str] = []
    for path in sorted(repo_dir.glob("mongo_*.py")):
        if path.name in ALLOWED_FILES:
            continue
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _PATTERN.search(line)
            if not match:
                continue
            # Accept calls made through ``self._coll`` (an
            # ``OrgScopedCollection``) and through the awaited return
            # of ``OrgScopedCollection`` helpers such as
            # ``_coll.find_many`` / ``_coll.find_one`` — those are the
            # wrapper's own method names, which happen to collide
            # with motor's names but live on a different class.
            head = line[: match.start()]
            if head.rstrip().endswith("self._coll") or head.rstrip().endswith(
                "self._upstreams"
            ) or head.rstrip().endswith("self._memberships"):
                continue
            # The wrapper's own ``find_many`` helper is fine too.
            if "find_many" in match.group(0):
                continue
            offenders.append(
                f"{path.name}:{lineno}: {stripped}"
            )
    assert not offenders, (
        "Raw Mongo access bypassing OrgScopedCollection:\n"
        + "\n".join(offenders)
    )
