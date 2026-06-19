"""Adversarial ``target_path`` confinement for materialize-files.

[BUG?] Sandbox files are written via ``MaterializeFile.target_path``,
which is operator-controlled (it carries the ``${HOME}/${FILE_NAME}``
substitution result). A path that points OUTSIDE the session's sandbox
home — a ``..`` traversal (``${HOME}/../../etc/x``) or an absolute path
to a system location (``/etc/x``) — must be rejected, or confined to
the session home, before any write happens. Otherwise the write
escapes the sandbox boundary.

Run against BOTH backends (E2B mock + local-subprocess). On the E2B
mock the escape is observable as a ``write_file`` recorded with a
path outside ``sandbox_home``; on local-subprocess it is a real
``open(path)`` that lands a file on the host filesystem outside the
per-session temp home — the failing test IS the proof-of-concept.

The two backends share no confinement helper today, so each gets its
own guardrail. Both assert the same intended contract: the write never
lands outside ``service.sandbox_home(session_id=…)``.
"""
from __future__ import annotations

import os

import pytest

from mcpolis.adapters.sandbox_services import LocalSubprocessSandboxService
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    SandboxResources,
)
from tests.unit.factories import make_upstream_definition
from tests.unit.sandbox_e2b_mock import make_mock_e2b_client
from tests.unit.test_e2b_sandbox_service import (
    make_default_resources,
    make_e2b_service,
)


def _is_confined(path: str, home: str) -> bool:
    """Return whether ``path`` resolves inside ``home``.

    Uses ``os.path.normpath`` + ``commonpath`` so a ``..`` traversal
    that climbs above ``home`` (or an absolute path elsewhere) reads
    as NOT confined.
    """
    home_norm = os.path.normpath(home)
    path_norm = os.path.normpath(
        path if os.path.isabs(path) else os.path.join(home, path),
    )
    try:
        return os.path.commonpath([home_norm, path_norm]) == home_norm
    except ValueError:
        # Different drives / mix of absolute+relative on Windows; treat
        # as not confined.
        return False


def _local_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


# ---------- E2B mock backend ----------


@pytest.mark.asyncio
async def test_e2b_materialize_rejects_absolute_path_outside_home() -> None:
    """[BUG?] Intended: an absolute ``target_path`` pointing at a
    system location (``/etc/cron.d/x``) is rejected or confined to the
    session home — never written verbatim. Observed: the SDK
    ``write_file`` receives the escaping path as-is."""
    client = make_mock_e2b_client()
    service, mock = make_e2b_service(client=client)
    home = service.sandbox_home(session_id="s1")
    upstream = make_upstream_definition(id="ups", command="npx")
    evil = MaterializeFile(
        name="cron", contents="* * * * * root touch /tmp/pwned\n",
        target_path="/etc/cron.d/pwned",
    )
    with pytest.raises(Exception):  # noqa: B017 - contract is "rejected"
        async with service.session(
            session_id="s1", org_id="acme", upstream=upstream,
            resources=make_default_resources(), denylist=(),
            materialize_files=[evil],
        ):
            pass
    # If it didn't raise, the write must at least have been confined.
    for w in mock.file_writes:
        assert _is_confined(w.path, home), (
            f"write escaped sandbox home: {w.path!r} not under {home!r}"
        )


@pytest.mark.asyncio
async def test_e2b_materialize_rejects_dotdot_traversal() -> None:
    """[BUG?] Intended: a ``..`` traversal that climbs above the
    sandbox home is rejected or confined. Observed: the path is passed
    through to ``write_file`` unchanged, so it escapes."""
    client = make_mock_e2b_client()
    service, mock = make_e2b_service(client=client)
    home = service.sandbox_home(session_id="s1")
    upstream = make_upstream_definition(id="ups", command="npx")
    evil = MaterializeFile(
        name="escape", contents="secret\n",
        target_path=f"{home}/../../../etc/escape",
    )
    with pytest.raises(Exception):  # noqa: B017 - contract is "rejected"
        async with service.session(
            session_id="s1", org_id="acme", upstream=upstream,
            resources=make_default_resources(), denylist=(),
            materialize_files=[evil],
        ):
            pass
    for w in mock.file_writes:
        assert _is_confined(w.path, home), (
            f"write escaped sandbox home: {w.path!r} not under {home!r}"
        )


# ---------- local-subprocess backend ----------


@pytest.mark.asyncio
async def test_local_subprocess_materialize_rejects_path_outside_home(
    tmp_path: object,
) -> None:
    """[BUG?] Intended: a ``target_path`` outside the per-session
    sandbox home is rejected or confined — never a real host write
    outside it. Observed: the write lands at the escaping path on the
    host. We aim it at a ``tmp_path`` sentinel (NOT a real system file)
    so the PoC proves the escape without damaging the host.
    """
    service = LocalSubprocessSandboxService()
    session_id = "traversal-local"
    home = service.sandbox_home(session_id=session_id)
    # Sentinel target deliberately OUTSIDE the per-session home — under
    # the test's tmp_path, so a successful escape is observable and
    # harmless.
    escape_target = os.path.join(str(tmp_path), "escaped-cred.txt")  # type: ignore[arg-type]
    assert not _is_confined(escape_target, home)
    upstream = make_upstream_definition(id="local-mcp", command="cat")
    evil = MaterializeFile(
        name="cred", contents="escaped-secret-body",
        target_path=escape_target,
    )
    try:
        with pytest.raises(Exception):  # noqa: B017 - contract is "rejected"
            async with service.session(
                session_id=session_id, org_id="org", upstream=upstream,
                resources=_local_resources(), denylist=(),
                materialize_files=[evil],
            ):
                pass
        # If no raise, the file must NOT exist outside the home.
        assert not os.path.exists(escape_target), (
            f"materialize wrote outside the sandbox home: {escape_target!r}"
        )
    finally:
        if os.path.exists(escape_target):
            os.remove(escape_target)
