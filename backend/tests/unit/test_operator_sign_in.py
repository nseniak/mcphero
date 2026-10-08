"""An MCP Hero operator browsing an org they are not a member of may use
its admin pages, but cannot sign in to its MCPs: only members' sign-ins
land, and the upstream's callback refuses anyone else. Connect says so at
once, instead of sending the operator through a consent page whose
result is then thrown away."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.dashboard_auth import build_session_cookie
from tests.unit.test_dashboard_api import make_test_client

OPERATOR = "op@mcphero.io"
ADMIN = "admin@example.com"
REFUSAL = "Only members of this organization can sign in to its MCPs"


def make_operator_client(tmp_path: Path) -> TestClient:
    """Signed in as an MCP Hero operator, browsing the ``default`` org,
    which they are not a member of."""
    client = make_test_client(tmp_path, login=None, superadmin_emails=OPERATOR)
    client.cookies.set("mcpolis_session", build_session_cookie(
        Settings(_env_file=None, session_secret="test-session-secret"),  # type: ignore[call-arg]
        email=OPERATOR, org_slug="default",
    ))
    return client


def coordinator_of(client: TestClient) -> PendingAuthCoordinator:
    return client.app.state.auth_coordinator  # type: ignore[attr-defined,no-any-return]


def test_an_operator_cannot_start_an_admin_sign_in(tmp_path: Path) -> None:
    client = make_operator_client(tmp_path)

    resp = client.post("/api/admin/upstreams/mixpanel/connect")

    assert resp.status_code == 409, resp.text
    assert REFUSAL in resp.json()["detail"]
    assert coordinator_of(client).get_pending("default", "mixpanel", OPERATOR) is None


def test_an_operator_cannot_start_a_personal_sign_in(tmp_path: Path) -> None:
    client = make_operator_client(tmp_path)

    resp = client.get("/api/auth/connect/mixpanel")

    assert resp.status_code == 409, resp.text
    assert REFUSAL in resp.json()["detail"]
    assert coordinator_of(client).get_pending("default", "mixpanel", OPERATOR) is None


def test_an_operator_may_start_a_stopped_mcp_from_the_sign_in_it_kept(
    tmp_path: Path,
) -> None:
    """Start reuses the admin sign-in Stop kept, whoever clicks it: no
    sign-in of the operator's own is needed, so none is refused."""
    client = make_operator_client(tmp_path)
    store = FileConnectionStore(tmp_path / "data")
    asyncio.run(store.put_user_token("default", ADMIN, "mixpanel", OAuthToken(
        access_token="kept", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1), scopes=[],
    )))
    stopped = client.post("/api/admin/upstreams/mixpanel/disconnect")
    assert stopped.status_code == 200, stopped.text

    resp = client.post("/api/admin/upstreams/mixpanel/connect")

    assert resp.status_code == 200, resp.text
    assert REFUSAL not in resp.text
