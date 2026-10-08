"""The Free plan refuses a sandbox bigger than its one allowed size.

Free allows exactly one sandbox size for a hosted stdio MCP (1 vCPU /
1024 MB). Picking anything bigger, when adding the MCP or when resizing
it later, must be refused with the plan-limit answer (HTTP 402) and must
leave nothing saved. The Team plan has no size limit, so the same request
goes through.

The dashboard tests drive the real add / edit routes on a standalone app
whose plan is seeded on disk. Standalone test settings carry no E2B key,
so the active sandbox provider is local-subprocess, whose size grid
accepts every size used here. That way the provider's own size check
passes and the plan rule is the only thing that can refuse the request.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.services.plan_gates import assert_sandbox_combo_allowed
from mcpolis.domain.services.plan_policy import PlanLimitExceeded
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as
from tests.unit.factories import make_config_users_accepted

ADMIN_EMAIL = "admin@example.com"
SANDBOX_GATE = "allowed_sandbox_combos"
SANDBOX_REFUSAL = "This sandbox size isn't available on your plan yet."

# (cpu_vcpus, memory_mb) sizes larger than Free's single allowed size.
# Both exist on the local-subprocess grid AND on the E2B template grid,
# so they are sizes a real admin can pick in the dashboard.
BIGGER_THAN_FREE: list[tuple[float, int]] = [
    (1.0, 2048),  # more memory
    (2.0, 2048),  # more CPU and memory
]


def make_settings(tmp_path: Path, plan: PlanName) -> Settings:
    """Standalone settings for one admin, no MCPs yet, on ``plan``."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }))
    data_dir = tmp_path / "data"
    # Every user accepted their invitation: a pending one gives no access.
    make_config_users_accepted(data_dir, config.read_text())
    # Standalone defaults the lone org to Team, so the plan is seeded
    # explicitly (the file the org repository reads at startup).
    (data_dir / "subscription.json").write_text(
        json.dumps({"plan": plan.value}),
    )
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
        allow_stdio_mcp=True,
    )


def make_admin_client(tmp_path: Path, plan: PlanName) -> TestClient:
    """A dashboard client signed in as the org admin, on ``plan``."""
    app = create_app(make_settings(tmp_path, plan))
    client = TestClient(app, raise_server_exceptions=True)
    login_as(client, ADMIN_EMAIL)
    return client


def make_add_hosted_mcp_body(
    mcp_id: str, size: tuple[float, int] | None,
) -> dict[str, object]:
    """Body of the dashboard "add hosted stdio MCP" request. ``size``
    ``None`` means the admin did not touch the size picker."""
    body: dict[str, object] = {
        "id": mcp_id,
        "display_name": mcp_id.title(),
        "command": "echo",
        "args": ["hello"],
        "auth_mode": "service_account",
    }
    if size is not None:
        body["cpu_vcpus"] = size[0]
        body["memory_mb"] = size[1]
    return body


def read_sandbox_size(client: TestClient, mcp_id: str) -> tuple[float, int]:
    """The sandbox size the MCP detail page shows."""
    resp = client.get(f"/api/admin/upstreams/{mcp_id}")
    assert resp.status_code == 200, resp.text
    sandbox = resp.json()["sandbox_resources"]
    return (sandbox["cpu_vcpus"], sandbox["memory_mb"])


def assert_sandbox_size_refusal(body: dict[str, object]) -> None:
    """The plan-limit answer the upgrade dialog reads."""
    assert body["error"] == "plan_limit_exceeded"
    assert body["gate"] == SANDBOX_GATE
    assert body["message"] == SANDBOX_REFUSAL


# --- Adding a hosted MCP ----------------------------------------------


@pytest.mark.parametrize("size", BIGGER_THAN_FREE)
def test_free_plan_refuses_adding_a_hosted_mcp_with_a_bigger_sandbox(
    tmp_path: Path, size: tuple[float, int],
) -> None:
    """Free: adding a hosted MCP with a sandbox bigger than 1 vCPU /
    1024 MB is refused with the plan-limit answer, and no MCP is saved."""
    client = make_admin_client(tmp_path, PlanName.free)

    resp = client.post(
        "/api/admin/upstreams", json=make_add_hosted_mcp_body("big", size),
    )

    assert resp.status_code == 402, resp.text
    assert_sandbox_size_refusal(resp.json())
    assert client.get("/api/admin/upstreams/big").status_code == 404


def test_free_plan_allows_adding_a_hosted_mcp_with_its_own_sandbox_size(
    tmp_path: Path,
) -> None:
    """Free: explicitly picking the one allowed size (1 vCPU / 1024 MB)
    is accepted, so the refusal above is about the size, not about
    hosted MCPs or the size picker."""
    client = make_admin_client(tmp_path, PlanName.free)

    resp = client.post(
        "/api/admin/upstreams",
        json=make_add_hosted_mcp_body("small", (1.0, 1024)),
    )

    assert resp.status_code == 201, resp.text
    assert read_sandbox_size(client, "small") == (1.0, 1024)


@pytest.mark.parametrize("size", BIGGER_THAN_FREE)
def test_team_plan_allows_adding_a_hosted_mcp_with_a_bigger_sandbox(
    tmp_path: Path, size: tuple[float, int],
) -> None:
    """Team: the same bigger sandbox is accepted and saved as picked."""
    client = make_admin_client(tmp_path, PlanName.team)

    resp = client.post(
        "/api/admin/upstreams", json=make_add_hosted_mcp_body("big", size),
    )

    assert resp.status_code == 201, resp.text
    assert read_sandbox_size(client, "big") == size


# --- Resizing an existing hosted MCP ----------------------------------


@pytest.mark.parametrize("size", BIGGER_THAN_FREE)
def test_free_plan_refuses_resizing_a_hosted_mcp_to_a_bigger_sandbox(
    tmp_path: Path, size: tuple[float, int],
) -> None:
    """Free: resizing an existing hosted MCP to a bigger sandbox is
    refused with the plan-limit answer, and the saved size stays at
    1 vCPU / 1024 MB."""
    client = make_admin_client(tmp_path, PlanName.free)
    created = client.post(
        "/api/admin/upstreams", json=make_add_hosted_mcp_body("tool", None),
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/admin/upstreams/tool",
        json={"sandbox_resources": {"cpu_vcpus": size[0], "memory_mb": size[1]}},
    )

    assert resp.status_code == 402, resp.text
    assert_sandbox_size_refusal(resp.json())
    assert read_sandbox_size(client, "tool") == (1.0, 1024)


@pytest.mark.parametrize("size", BIGGER_THAN_FREE)
def test_team_plan_allows_resizing_a_hosted_mcp_to_a_bigger_sandbox(
    tmp_path: Path, size: tuple[float, int],
) -> None:
    """Team: the same resize is accepted and the new size is saved."""
    client = make_admin_client(tmp_path, PlanName.team)
    created = client.post(
        "/api/admin/upstreams", json=make_add_hosted_mcp_body("tool", None),
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/admin/upstreams/tool",
        json={"sandbox_resources": {"cpu_vcpus": size[0], "memory_mb": size[1]}},
    )

    assert resp.status_code == 200, resp.text
    assert read_sandbox_size(client, "tool") == size


# --- The plan rule itself ---------------------------------------------


@pytest.mark.parametrize("size", BIGGER_THAN_FREE)
def test_sandbox_size_rule_refuses_a_bigger_size_on_free(
    size: tuple[float, int],
) -> None:
    """The shared plan rule (also used by the admin MCP tools) refuses a
    bigger size on Free with the error the 402 answer is built from."""
    with pytest.raises(PlanLimitExceeded) as excinfo:
        assert_sandbox_combo_allowed(
            PlanName.free, size[0], size[1],
            source="test", org_id="default", actor_email=ADMIN_EMAIL,
        )

    assert excinfo.value.gate == SANDBOX_GATE
    assert excinfo.value.message == SANDBOX_REFUSAL


def test_sandbox_size_rule_allows_free_its_own_size() -> None:
    """The shared plan rule lets Free use its one size, 1 vCPU / 1024 MB."""
    assert_sandbox_combo_allowed(
        PlanName.free, 1.0, 1024,
        source="test", org_id="default", actor_email=ADMIN_EMAIL,
    )


@pytest.mark.parametrize("size", BIGGER_THAN_FREE)
def test_sandbox_size_rule_allows_a_bigger_size_on_team(
    size: tuple[float, int],
) -> None:
    """The shared plan rule lets Team use a bigger size."""
    assert_sandbox_combo_allowed(
        PlanName.team, size[0], size[1],
        source="test", org_id="default", actor_email=ADMIN_EMAIL,
    )
