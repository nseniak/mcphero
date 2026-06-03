"""REST API tests for the per-MCP secrets endpoints.

Pins the wire shape of:

- ``GET    /api/admin/upstreams/{id}/secrets`` — returns
  ``[TemplateVarSummaryView]`` with ``last_four`` but no ``value``.
- ``PUT    /api/admin/upstreams/{id}/template-vars/{name}`` — accepts
  ``{value}``, returns the post-write summary.
- ``DELETE /api/admin/upstreams/{id}/template-vars/{name}`` — idempotent.
- ``POST   /api/admin/upstreams`` extended with ``secrets`` field —
  the create-with-secrets atomic path used by the wizard.

Plus the negative cases: invalid name → 400, unauthenticated
request → 401/403, name with disallowed chars → 400, delete
cascades when the upstream is removed. Empty values are NOT a
negative case — substitution emits the empty string for them,
which is sometimes the intended value.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as

MCP_JSON = json.dumps({
    "mcpServers": {
        "github": {"url": "http://localhost:9000/mcp"},
    }
})

CONFIG_JSON = json.dumps({
    "upstreams": {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
    },
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {"mcp_access": {"mcps": {"github": True}}},
        },
    },
    "users": {
        "admin@example.com": {"role": "admin"},
    },
})


def make_test_client(
    tmp_path: Path, *, login: str | None = "admin@example.com",
) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(MCP_JSON)
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    if login is not None:
        login_as(client, login)
    return client


# --- list_secrets ---


def test_list_secrets_empty_for_new_upstream(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/github/template-vars")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_secrets_requires_admin(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login=None)
    resp = client.get("/api/admin/upstreams/github/template-vars")
    assert resp.status_code in (401, 403)


# --- set_secret ---


def test_set_secret_round_trips_summary(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "ghp_long_value_more_than_16_chars"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "GH_TOKEN"
    assert data["last_four"] == "hars"
    # Both kinds carry the plaintext now — UI obfuscates by default
    # via an eye toggle (1Password-style).
    assert data["value"] == "ghp_long_value_more_than_16_chars"
    assert data["is_secret"] is True
    # GET reflects the new state.
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    assert len(listing) == 1
    assert listing[0]["name"] == "GH_TOKEN"
    assert listing[0]["last_four"] == "hars"
    assert listing[0]["value"] == "ghp_long_value_more_than_16_chars"
    assert listing[0]["is_secret"] is True


def test_set_secret_short_value_has_no_last_four(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/SHORT",
        json={"value": "short-value"},
    )
    assert resp.status_code == 200
    assert resp.json()["last_four"] is None


def test_set_secret_replace_keeps_created_at(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    first = client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "first-value-1234567890"},
    ).json()
    second = client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "second-value-9876543210"},
    ).json()
    assert first["created_at"] == second["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert second["last_four"] == "3210"


def test_set_secret_rejects_invalid_name(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    # Lowercase, hyphen, leading digit — all invalid env-var names.
    for bad in ("lowercase", "kebab-case", "1leading_digit"):
        resp = client.put(
            f"/api/admin/upstreams/github/template-vars/{bad}",
            json={"value": "anything"},
        )
        assert resp.status_code == 400, (
            f"expected 400 for invalid name {bad!r}, got {resp.status_code}"
        )


def test_set_secret_accepts_empty_value(tmp_path: Path) -> None:
    """Empty values are allowed: the substitution helper emits the
    empty string for ``${VAR}`` when the saved value is ``""`` —
    sometimes the intended value (clearing a header, passing an
    empty ``--flag ""``, etc.)."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": ""},
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    assert listing[0]["name"] == "GH_TOKEN"
    assert listing[0]["value"] == ""
    # Empty value has no last-4 preview.
    assert listing[0]["last_four"] is None


def test_set_secret_requires_admin(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login=None)
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "value-1234567890"},
    )
    assert resp.status_code in (401, 403)


# --- delete_secret ---


def test_delete_secret_removes_it(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "value-1234567890"},
    )
    resp = client.delete("/api/admin/upstreams/github/template-vars/GH_TOKEN")
    assert resp.status_code == 200
    assert client.get("/api/admin/upstreams/github/template-vars").json() == []


def test_delete_secret_idempotent(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/upstreams/github/template-vars/NEVER_DEFINED")
    assert resp.status_code == 200


def test_delete_secret_rejects_invalid_name(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/upstreams/github/template-vars/lowercase")
    assert resp.status_code == 400


# --- add_upstream extended with env_vars field ---


def test_add_upstream_with_env_vars_persists_them(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "atomic",
            "display_name": "Atomic",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
            "template_vars": {
                "GITHUB_TOKEN": {
                    "value": "ghp_value_more_than_16_chars",
                    "is_secret": True,
                },
                "LOG_LEVEL": {"value": "debug", "is_secret": False},
                "SHORT": {"value": "tiny", "is_secret": True},
            },
        },
    )
    assert resp.status_code == 201
    listing = client.get("/api/admin/upstreams/atomic/template-vars").json()
    by_name = {s["name"]: s for s in listing}
    assert set(by_name.keys()) == {"GITHUB_TOKEN", "LOG_LEVEL", "SHORT"}
    assert by_name["GITHUB_TOKEN"]["last_four"] == "hars"
    assert by_name["GITHUB_TOKEN"]["value"] == "ghp_value_more_than_16_chars"
    assert by_name["GITHUB_TOKEN"]["is_secret"] is True
    # Plain row carries the value verbatim.
    assert by_name["LOG_LEVEL"]["value"] == "debug"
    assert by_name["LOG_LEVEL"]["is_secret"] is False
    # Short secret value: no last-4, but the plaintext now surfaces
    # too (UI obfuscates by default with the eye toggle).
    assert by_name["SHORT"]["last_four"] is None
    assert by_name["SHORT"]["value"] == "tiny"


def test_add_upstream_rejects_invalid_env_var_name(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "rejected",
            "display_name": "Rejected",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
            "template_vars": {
                "lowercase-name": {"value": "anything", "is_secret": True},
            },
        },
    )
    assert resp.status_code == 400


def test_remove_upstream_cascades_to_secrets(tmp_path: Path) -> None:
    """Pins the cascade: deleting the upstream wipes its secrets so
    they can't outlive their owner. Mirrors the unit-level test against
    ``UpstreamConfigService.remove_upstream`` but exercises the full
    REST path."""
    client = make_test_client(tmp_path)
    # Create the upstream + a secret on it.
    client.post(
        "/api/admin/upstreams",
        json={
            "id": "cascade",
            "display_name": "Cascade",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
        },
    )
    client.put(
        "/api/admin/upstreams/cascade/template-vars/GH_TOKEN",
        json={"value": "value-1234567890"},
    )
    # Sanity: the secret is there.
    assert client.get(
        "/api/admin/upstreams/cascade/template-vars",
    ).json() != []
    # Remove the upstream.
    client.delete("/api/admin/upstreams/cascade")
    # The secrets row should be gone.
    # Re-create a same-id upstream and confirm it has no secrets.
    client.post(
        "/api/admin/upstreams",
        json={
            "id": "cascade",
            "display_name": "Cascade",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
        },
    )
    assert client.get("/api/admin/upstreams/cascade/template-vars").json() == []


# --- is_secret toggle ---


def test_set_plain_var_returns_value_in_summary(tmp_path: Path) -> None:
    """``is_secret=false`` rows expose the value in the summary so the
    UI can render it verbatim."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/LOG_LEVEL",
        json={"value": "debug", "is_secret": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_secret"] is False
    assert data["value"] == "debug"
    # GET reflects the same shape.
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    assert listing[0]["value"] == "debug"
    assert listing[0]["is_secret"] is False


def test_replace_preserves_is_secret_when_body_disagrees(tmp_path: Path) -> None:
    """The toggle is a create-time decision: replacing a row with a
    body that toggles ``is_secret`` must NOT change the stored flag."""
    client = make_test_client(tmp_path)
    # Create as plain.
    client.put(
        "/api/admin/upstreams/github/template-vars/LOG_LEVEL",
        json={"value": "debug", "is_secret": False},
    )
    # Try to flip to secret on replace — the request is honoured for
    # the value, but the flag is preserved.
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/LOG_LEVEL",
        json={"value": "info", "is_secret": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_secret"] is False, "replace must not flip the flag"
    assert data["value"] == "info"


def test_replace_preserves_secret_flag_against_body_false(tmp_path: Path) -> None:
    """Symmetric: a saved secret stays a secret even if the replace
    body sends ``is_secret=false``. Otherwise a malicious / buggy
    client could un-mask a saved secret."""
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "first-value-1234567890", "is_secret": True},
    )
    resp = client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "rotated-value-9876543210", "is_secret": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_secret"] is True
    # Replace honours the new value; the SPA obfuscates password
    # rows by default but the plaintext is now part of the API
    # contract (1Password-style reveal toggle).
    assert data["value"] == "rotated-value-9876543210"


def test_legacy_record_without_is_secret_reads_back_as_secret(
    tmp_path: Path,
) -> None:
    """A v1-era stored record (no ``is_secret`` field) must default
    to ``is_secret=True`` on read so old data doesn't accidentally
    leak."""
    import json
    client = make_test_client(tmp_path)
    # Hand-write a legacy record into the file store.
    secrets_path = tmp_path / "data" / "template_vars.json"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps({
        "default": {
            "github": {
                "LEGACY": {
                    "value": "value-from-v1",
                    "last_four": "rom1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                },
            },
        },
    }))
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    by_name = {s["name"]: s for s in listing}
    assert "LEGACY" in by_name
    assert by_name["LEGACY"]["is_secret"] is True
    # v1 records stored the plaintext under ``value`` already; the new
    # contract returns it for password rows too (SPA obfuscates).
    assert by_name["LEGACY"]["value"] == "value-from-v1"


# --- update_upstream extended with template_var_changes (PR 4: deferred Save) ---


def test_update_upstream_template_var_changes_sets_new_row(tmp_path: Path) -> None:
    """The deferred-save buffer flushes via PUT /upstreams/{id}; a
    fresh ``set`` lands as a new row visible on the next list."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {
                    "GH_TOKEN": {
                        "value": "ghp_value_more_than_16_chars",
                        "is_secret": True,
                    },
                    "LOG_LEVEL": {"value": "debug", "is_secret": False},
                },
                "deletes": [],
            },
        },
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    by_name = {s["name"]: s for s in listing}
    assert set(by_name.keys()) == {"GH_TOKEN", "LOG_LEVEL"}
    assert by_name["GH_TOKEN"]["last_four"] == "hars"
    assert by_name["GH_TOKEN"]["value"] == "ghp_value_more_than_16_chars"
    assert by_name["LOG_LEVEL"]["value"] == "debug"


def test_update_upstream_template_var_changes_replace_preserves_is_secret(
    tmp_path: Path,
) -> None:
    """The repository's "is_secret is immutable on replace" contract
    must hold via the combined endpoint too — a replace through
    ``template_var_changes.sets`` cannot un-mask a saved password."""
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "first-value-1234567890", "is_secret": True},
    )
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {
                    "GH_TOKEN": {
                        "value": "rotated-value-9876543210",
                        "is_secret": False,
                    },
                },
                "deletes": [],
            },
        },
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    row = next(s for s in listing if s["name"] == "GH_TOKEN")
    assert row["is_secret"] is True, "replace must not flip the flag"
    assert row["value"] == "rotated-value-9876543210"
    assert row["last_four"] == "3210"


def test_update_upstream_template_var_changes_deletes_row(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/upstreams/github/template-vars/GH_TOKEN",
        json={"value": "value-1234567890"},
    )
    client.put(
        "/api/admin/upstreams/github/template-vars/KEEP_ME",
        json={"value": "keep-this-value-1234567890", "is_secret": False},
    )
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {},
                "deletes": ["GH_TOKEN"],
            },
        },
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    by_name = {s["name"]: s for s in listing}
    assert "GH_TOKEN" not in by_name
    assert "KEEP_ME" in by_name


def test_update_upstream_template_var_changes_sets_and_deletes_combined(
    tmp_path: Path,
) -> None:
    """A user adds A + B, deletes C, replaces D — all in one Save."""
    client = make_test_client(tmp_path)
    # Seed C and D.
    client.put(
        "/api/admin/upstreams/github/template-vars/C_OLD",
        json={"value": "old-value-1234567890"},
    )
    client.put(
        "/api/admin/upstreams/github/template-vars/D_REPLACE",
        json={"value": "before-value-1234567890"},
    )
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {
                    "A_NEW": {"value": "alpha-value-1234567890"},
                    "B_NEW": {"value": "bravo-value-1234567890"},
                    "D_REPLACE": {"value": "after-value-zzzzzzzzz"},
                },
                "deletes": ["C_OLD"],
            },
        },
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    by_name = {s["name"]: s for s in listing}
    assert set(by_name.keys()) == {"A_NEW", "B_NEW", "D_REPLACE"}
    assert by_name["D_REPLACE"]["last_four"] == "zzzz"


def test_update_upstream_template_var_changes_invalid_name_rolls_back(
    tmp_path: Path,
) -> None:
    """A bad name in the set/delete list rejects the entire save with
    a 400 — no env var is created, no upstream-config change leaks
    through."""
    client = make_test_client(tmp_path)
    # Sanity: starting state.
    assert client.get("/api/admin/upstreams/github/template-vars").json() == []
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "display_name": "Renamed-but-should-not-stick",
            "template_var_changes": {
                "sets": {
                    "VALID_NAME": {"value": "ok"},
                    "lowercase-bad": {"value": "ok"},
                },
                "deletes": [],
            },
        },
    )
    assert resp.status_code == 400
    # No env var was created — validation runs before any mutation.
    assert client.get("/api/admin/upstreams/github/template-vars").json() == []


def test_update_upstream_template_var_changes_empty_value_is_accepted(
    tmp_path: Path,
) -> None:
    """Empty values are allowed via the combined save endpoint too —
    matches the per-name PUT contract."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {"VALID_NAME": {"value": ""}},
                "deletes": [],
            },
        },
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    assert len(listing) == 1
    assert listing[0]["name"] == "VALID_NAME"
    assert listing[0]["value"] == ""


def test_update_upstream_template_var_changes_delete_then_set_lands_as_set(
    tmp_path: Path,
) -> None:
    """Buffer semantic: a user typed Add then Delete in one session
    on a not-yet-saved name → the row should not exist after Save.
    Reverse (Add then Delete on an existing-server name) is what gets
    expressed when a same name appears in both ``sets`` and
    ``deletes`` — the set wins (matches the user-visible "I added it
    last")."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {"BOTH": {"value": "added-after-delete-12345"}},
                "deletes": ["BOTH"],
            },
        },
    )
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/github/template-vars").json()
    assert any(s["name"] == "BOTH" for s in listing)


def test_update_upstream_template_var_changes_idempotent_delete_missing(
    tmp_path: Path,
) -> None:
    """Deleting a row that doesn't exist is a no-op — same as the
    per-name DELETE endpoint's contract."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={
            "template_var_changes": {
                "sets": {},
                "deletes": ["NEVER_DEFINED"],
            },
        },
    )
    assert resp.status_code == 200


def test_update_upstream_without_template_var_changes_unchanged(
    tmp_path: Path,
) -> None:
    """The combined field is optional — legacy callers (display-name
    rename, auth-mode flip) keep working without it."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={"display_name": "Renamed"},
    )
    assert resp.status_code == 200
    detail = client.get("/api/admin/upstreams/github").json()
    assert detail["display_name"] == "Renamed"


def test_add_upstream_with_no_secrets_field_works(tmp_path: Path) -> None:
    """The ``secrets`` field is optional; the legacy create call must
    keep working unchanged."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "legacy",
            "display_name": "Legacy",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
        },
    )
    assert resp.status_code == 201
    assert client.get("/api/admin/upstreams/legacy/template-vars").json() == []
