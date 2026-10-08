"""The super-admin "kick out everywhere" action
(``POST /api/superadmin/users/{email}/sessions/revoke``): revokes the
person's gateway tokens and closes their open gateway sessions in every
org they belong to, and reports both counts.

Uses the standalone harness of ``test_superadmin_upstream_liveness`` (one
org, ``default``) and the gateway helpers of ``test_dashboard_api``.
"""
from __future__ import annotations

from pathlib import Path

from tests.unit.test_dashboard_api import (
    add_open_gateway_session,
    seed_gateway_tokens,
)
from tests.unit.test_superadmin_upstream_liveness import (
    SUPERADMIN_EMAIL,
    make_settings_with_upstreams,
    make_superadmin_client,
)


def test_superadmin_revoke_ends_tokens_and_open_sessions(tmp_path: Path) -> None:
    client = make_superadmin_client(
        make_settings_with_upstreams(tmp_path, upstreams={}, mcp_servers={}),
    )
    seed_gateway_tokens(client, SUPERADMIN_EMAIL)
    terminate = add_open_gateway_session(client, SUPERADMIN_EMAIL, "s-1")

    resp = client.post(
        f"/api/superadmin/users/{SUPERADMIN_EMAIL}/sessions/revoke",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "email": SUPERADMIN_EMAIL,
        "gateway_tokens_revoked": 3,
        "upstream_sessions_terminated": 1,
        "orgs_touched": 1,
    }
    terminate.assert_awaited_once()
