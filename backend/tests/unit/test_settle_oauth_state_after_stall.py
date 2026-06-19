"""BUG-9 — `settle_oauth_state_after_stall` gating unit guardrails.

The dead-token reconnect probe the non-retry-safe tool-call stall path
runs. These pin the DECISION logic with dependency injection (a real
instrumented FileConnectionStore + a real UpstreamClientManager, no
patching): the probe must run only for OAuth upstreams, never for
service_account (which has no OAuth tokens) and never without a
connection store. The full dead-token deletion end-state is proven by
the 39b e2e + the `_classify_reconnect_failure` tests in
``test_refresh_failure_policy.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.upstream_connection_service import (
    settle_oauth_state_after_stall,
)
from tests.unit.factories import make_upstream_auth, make_upstream_definition


class _RecordingStore(FileConnectionStore):
    """A real file-backed store that records the `(user_id)` of every
    `get_user_token` lookup — so a test can prove whether the reconnect
    probe actually consulted the store for a given slot owner."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.get_token_users: list[str] = []

    async def get_user_token(
        self, org_id: str, user_id: str, upstream_id: str,
    ) -> OAuthToken | None:
        self.get_token_users.append(user_id)
        return await super().get_user_token(org_id, user_id, upstream_id)


def make_oauth_upstream(upstream_id: str = "ups") -> UpstreamDefinition:
    return make_upstream_definition(
        id=upstream_id,
        auth=make_upstream_auth(mode=AuthMode.per_user_oauth),
    )


@pytest.mark.asyncio
async def test_settle_skips_service_account(tmp_path: Path) -> None:
    """service_account has no OAuth tokens — the probe must NOT run (it
    would otherwise risk classifying/deleting a non-existent token row)."""
    upstream = make_upstream_definition(id="svc")  # service_account / stdio
    store = _RecordingStore(tmp_path)
    cm = UpstreamClientManager([upstream])

    await settle_oauth_state_after_stall(
        org_id="o", upstream=upstream, effective_user="",
        connection_store=store, client_manager=cm, server_url="http://x",
    )

    assert store.get_token_users == []  # reconnect probe never ran


@pytest.mark.asyncio
async def test_settle_skips_when_no_connection_store(tmp_path: Path) -> None:
    """No connection store wired (e.g. OAuth not configured) — the probe
    must no-op cleanly, never raise."""
    upstream = make_oauth_upstream()
    cm = UpstreamClientManager([upstream])

    await settle_oauth_state_after_stall(
        org_id="o", upstream=upstream, effective_user="alice",
        connection_store=None, client_manager=cm, server_url="http://x",
    )


@pytest.mark.asyncio
async def test_settle_runs_probe_for_oauth_upstream(tmp_path: Path) -> None:
    """An OAuth upstream runs the reconnect probe for the slot owner. With
    an empty store the probe finds no tokens and completes cleanly (no
    raise, no deletion) — but it MUST have consulted the store for the
    given effective_user, proving it did not gate out."""
    upstream = make_oauth_upstream()
    store = _RecordingStore(tmp_path)
    cm = UpstreamClientManager([upstream])

    await settle_oauth_state_after_stall(
        org_id="o", upstream=upstream, effective_user="alice",
        connection_store=store, client_manager=cm, server_url="http://x",
    )

    assert "alice" in store.get_token_users
