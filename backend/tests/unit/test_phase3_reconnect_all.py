"""Phase 3 — startup reconnect uses the admin pool, skips per_user_oauth.

After Phase 2 every admin's ``admin_oauth`` token is stored under the
admin's real email. The startup ``reconnect_all_oauth_upstreams``
sweep iterates admin emails (plus the legacy ``ADMIN_USER_ID`` slot
as a fall-through for pre-Phase-2 data) and tries each one's stored
token in turn so an org with multiple admins is resilient to one
having a stale row.

``per_user_oauth`` upstreams are skipped — per-user tokens reconnect
lazily on each user's first invocation. The tool catalog itself
survives restart via ``ToolRegistry.hydrate`` (Phase 0), so the UI
works even if no upstream reconnects this boot.

These tests stub ``reconnect_with_stored_tokens`` to capture the
call sequence, then assert the right user_ids are tried in order
without going through real OAuth machinery.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import upstream_connection_service as uc
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    DisconnectReason,
    reconnect_all_oauth_upstreams,
)

from tests.unit.factories import make_upstream_auth, make_upstream_definition


def make_oauth_upstream(
    upstream_id: str = "slack",
    auth_mode: AuthMode = AuthMode.admin_oauth,
):
    from mcpolis.domain.model.upstream import TransportType

    return make_upstream_definition(
        id=upstream_id,
        transport=TransportType.streamable_http,
        url="http://localhost:9999/mcp",
        auth=make_upstream_auth(mode=auth_mode),
    )


def make_capturing_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    outcome_per_user: dict[str, DisconnectReason | None],
) -> list[tuple[str, str]]:
    """Stub ``reconnect_with_stored_tokens`` and capture each call.

    *outcome_per_user* maps ``user_id`` → outcome (``None`` for success).
    Returns the list of ``(upstream_id, user_id)`` tuples, mutated as
    calls are made so tests can assert call ordering.
    """
    calls: list[tuple[str, str]] = []

    async def _stub(
        org_id: str,
        upstream,
        effective_user: str,
        connection_store,
        client_manager,
        server_url: str,
    ) -> DisconnectReason | None:
        calls.append((upstream.id, effective_user))
        return outcome_per_user.get(
            effective_user, DisconnectReason.no_tokens,
        )

    monkeypatch.setattr(uc, "reconnect_with_stored_tokens", _stub)
    return calls


def make_registry_stub() -> MagicMock:
    registry = MagicMock(spec=ToolRegistry)
    from unittest.mock import AsyncMock as _AsyncMock

    registry.refresh_upstream = _AsyncMock()
    return registry


def make_client_manager() -> MagicMock:
    return MagicMock(spec=UpstreamClientManager)


@pytest.mark.asyncio
async def test_admin_oauth_tries_each_admin_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = make_oauth_upstream(auth_mode=AuthMode.admin_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    # No user has tokens — every reconnect call returns no_tokens.
    calls = make_capturing_reconnect(monkeypatch, outcome_per_user={})

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com", "bob@co.com"],
    )

    assert calls == [
        ("slack", "alice@co.com"),
        ("slack", "bob@co.com"),
    ]
    # all-no-tokens is filtered out (no surfacing reason).
    assert reasons == {}


@pytest.mark.asyncio
async def test_admin_oauth_first_successful_admin_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = make_oauth_upstream(auth_mode=AuthMode.admin_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    # alice succeeds first; bob/legacy must not be tried.
    calls = make_capturing_reconnect(
        monkeypatch,
        outcome_per_user={"alice@co.com": None},
    )

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com", "bob@co.com"],
    )

    assert calls == [("slack", "alice@co.com")]
    assert reasons == {}
    registry.refresh_upstream.assert_awaited_once_with("slack")


@pytest.mark.asyncio
async def test_admin_oauth_skips_stale_admin_falls_over_to_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale admin entry (no_tokens) doesn't block the sweep — bob's
    valid token still gets connected."""
    upstream = make_oauth_upstream(auth_mode=AuthMode.admin_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    calls = make_capturing_reconnect(
        monkeypatch,
        outcome_per_user={"bob@co.com": None},
    )

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com", "bob@co.com"],
    )

    assert calls == [("slack", "alice@co.com"), ("slack", "bob@co.com")]
    assert reasons == {}


@pytest.mark.asyncio
async def test_admin_oauth_real_failure_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every candidate fails with a real reason (not just
    ``no_tokens``), the worst non-no-tokens outcome is surfaced so the
    upstream's connection_error is set."""
    upstream = make_oauth_upstream(auth_mode=AuthMode.admin_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    calls = make_capturing_reconnect(
        monkeypatch,
        outcome_per_user={
            "alice@co.com": DisconnectReason.token_refresh_failed,
        },
    )

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com"],
    )

    assert "slack" in reasons
    assert reasons["slack"] == DisconnectReason.token_refresh_failed
    # Only admin pool is tried — no legacy fall-through after Phase 6.
    assert calls == [("slack", "alice@co.com")]


@pytest.mark.asyncio
async def test_per_user_oauth_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``per_user_oauth`` upstreams are not pre-warmed — they
    reconnect lazily on first user request. The sweep does not call
    reconnect_with_stored_tokens for them at all."""
    upstream = make_oauth_upstream(auth_mode=AuthMode.per_user_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    calls = make_capturing_reconnect(monkeypatch, outcome_per_user={})

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com"],
    )

    assert calls == []
    assert reasons == {}


@pytest.mark.asyncio
async def test_service_account_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = make_oauth_upstream(auth_mode=AuthMode.service_account)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    calls = make_capturing_reconnect(monkeypatch, outcome_per_user={})

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com"],
    )

    assert calls == []
    assert reasons == {}


@pytest.mark.asyncio
async def test_refresh_failure_does_not_abort_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boot-time ``refresh_upstream`` stall (e.g. a slow ``list_tools``
    hitting LIST_TOOLS_TIMEOUT) on one upstream must be swallowed, not
    bubbled. Before the fix it escaped ``reconnect_all_oauth_upstreams``,
    cancelled the sibling reconnects via the bare ``asyncio.gather``, and
    was mislabelled as a whole-org ``org.runtime.startup.failed`` by
    ``connect_runtime``'s catch-all (an ERROR-level Sentry alert).

    Here ``slack`` reconnects and then times out on refresh; ``github``
    must still reconnect and refresh, and the sweep must return normally
    with no surfaced reason for either (both sessions are connected).
    """
    slack = make_oauth_upstream("slack", auth_mode=AuthMode.admin_oauth)
    github = make_oauth_upstream("github", auth_mode=AuthMode.admin_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    # Both upstreams reconnect successfully for alice.
    make_capturing_reconnect(
        monkeypatch, outcome_per_user={"alice@co.com": None},
    )

    # slack's refresh stalls and times out; github's succeeds.
    async def _refresh(upstream_id: str) -> None:
        if upstream_id == "slack":
            raise TimeoutError("list_tools timed out")

    registry.refresh_upstream.side_effect = _refresh

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [slack, github], store, cm, registry,
        "http://localhost:8080",
        admin_emails=["alice@co.com"],
    )

    # The timeout was swallowed — the sweep returns normally with no
    # surfaced reason (both sessions connected; only slack's catalog
    # refresh was skipped).
    assert reasons == {}
    # Both upstreams' refresh was attempted — github was NOT cancelled
    # by slack's failure.
    refreshed = {
        call.args[0] for call in registry.refresh_upstream.await_args_list
    }
    assert refreshed == {"slack", "github"}


@pytest.mark.asyncio
async def test_no_admins_no_reconnect_attempted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An org with zero admin emails has no candidate user to
    reconnect with, so the sweep is a no-op. The upstream stays
    disconnected until an admin signs in."""
    upstream = make_oauth_upstream(auth_mode=AuthMode.admin_oauth)
    store = FileConnectionStore(tmp_path)
    cm = make_client_manager()
    registry = make_registry_stub()

    calls = make_capturing_reconnect(monkeypatch, outcome_per_user={})

    reasons = await reconnect_all_oauth_upstreams(
        DEFAULT_ORG_ID, [upstream], store, cm, registry,
        "http://localhost:8080",
        admin_emails=[],
    )

    assert calls == []
    assert reasons == {}
    registry.refresh_upstream.assert_not_awaited()
