"""Tests for upstream config loading (mcp.json + options merge)."""
from __future__ import annotations

import json
from pathlib import Path

from mcpolis.adapters.repositories.upstream_config_loader import (
    auto_detect_auth_mode,
    auto_display_name,
    extract_import_entries,
    load_merged_config,
)
from mcpolis.domain.model.policy import AuthMode


def test_load_url_only(tmp_path: Path) -> None:
    """URL-only MCP with no options gets sensible defaults."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "github": {"url": "https://mcp.github.com/sse"}
        }
    }))

    result = load_merged_config(mcp_json)
    assert len(result) == 1
    assert result[0].id == "github"
    assert result[0].display_name == "Github"
    assert result[0].auth.mode == AuthMode.per_user_oauth
    assert result[0].http is not None
    assert result[0].http.url == "https://mcp.github.com/sse"


def test_load_command_only(tmp_path: Path) -> None:
    """Command-based MCP defaults to service_account."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
            }
        }
    }))

    result = load_merged_config(mcp_json)
    assert len(result) == 1
    assert result[0].id == "filesystem"
    assert result[0].auth.mode == AuthMode.service_account
    assert result[0].stdio is not None
    assert result[0].stdio.command == "npx"
    assert result[0].stdio.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_load_with_bearer_token(tmp_path: Path) -> None:
    """URL with Bearer token in headers → service_account."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "private-mcp": {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer sk-secret"}
            }
        }
    }))

    result = load_merged_config(mcp_json)
    assert result[0].auth.mode == AuthMode.service_account
    assert result[0].auth.token == "sk-secret"


def test_options_override_display_name_and_auth(tmp_path: Path) -> None:
    """Options take precedence over auto-detected values."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "mixpanel": {"url": "https://mcp.mixpanel.com/mcp"}
        }
    }))
    options = {
        "mixpanel": {
            "display_name": "Mixpanel Analytics",
            "auth_mode": "admin_oauth",
        }
    }

    result = load_merged_config(mcp_json, options)
    assert result[0].display_name == "Mixpanel Analytics"
    assert result[0].auth.mode == AuthMode.admin_oauth


def test_options_override_tool_settings(tmp_path: Path) -> None:
    """Options can set default arguments."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "fs": {"command": "echo"}
        }
    }))
    options = {
        "fs": {
            "display_name": "Filesystem",
            "default_arguments": {"write_file": {"encoding": "utf-8"}},
        }
    }

    result = load_merged_config(mcp_json, options)
    assert result[0].default_arguments["write_file"]["encoding"] == "utf-8"


def test_no_mcp_json_returns_empty(tmp_path: Path) -> None:
    mcp_json = tmp_path / "mcp.json"
    assert load_merged_config(mcp_json) == []


def test_empty_mcp_json_returns_empty(tmp_path: Path) -> None:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text("{}")
    assert load_merged_config(mcp_json) == []


def test_multiple_mcps(tmp_path: Path) -> None:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "a": {"url": "https://a.com/mcp"},
            "b": {"command": "echo"},
            "c": {"url": "https://c.com/mcp"}
        }
    }))

    result = load_merged_config(mcp_json)
    assert len(result) == 3
    ids = {r.id for r in result}
    assert ids == {"a", "b", "c"}


def test_auto_detect_auth_mode_command() -> None:
    assert auto_detect_auth_mode({"command": "echo"}) == AuthMode.service_account


def test_auto_detect_auth_mode_url() -> None:
    assert auto_detect_auth_mode({"url": "https://example.com"}) == AuthMode.per_user_oauth


def test_auto_detect_auth_mode_bearer() -> None:
    assert auto_detect_auth_mode({
        "url": "https://example.com",
        "headers": {"Authorization": "Bearer tok"}
    }) == AuthMode.service_account


def test_auto_display_name() -> None:
    assert auto_display_name("github") == "Github"
    assert auto_display_name("my-mcp") == "My Mcp"
    assert auto_display_name("file_system") == "File System"


# --- extract_import_entries (bulk-import flattening) ---------------------


def test_extract_import_entries_standard_mcpservers() -> None:
    """A plain ``mcpServers`` blob → one standard-scope entry whose
    proposed id is the bare original id."""
    entries = extract_import_entries(
        {"mcpServers": {"github": {"url": "http://x/mcp"}}},
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.scope == "standard"
    assert entry.group_label == "Servers"
    assert entry.project_path is None
    assert entry.original_id == "github"
    assert entry.proposed_id == "github"
    assert entry.duplicate_of is None


def test_extract_import_entries_vscode_servers_key() -> None:
    entries = extract_import_entries(
        {"servers": {"foo": {"url": "http://x/mcp"}}},
    )
    assert [e.proposed_id for e in entries] == ["foo"]
    assert entries[0].scope == "standard"


def test_extract_import_entries_empty_when_unrecognized() -> None:
    assert extract_import_entries({"random": "junk"}) == []
    # A single top-level entry is not a config file.
    assert extract_import_entries({"url": "http://x/mcp"}) == []


def test_extract_import_entries_claude_json_union_and_grouping() -> None:
    """``.claude.json``: top-level (user) scope first, then each project
    in file order; project ids are prefixed with the project basename
    (scope-first, matching the product's ``{scope}-{leaf}`` convention)."""
    data = {
        "mcpServers": {"sentry": {"url": "http://sentry/mcp"}},
        "projects": {
            "/home/me/web": {"mcpServers": {"github": {"url": "http://gh/web"}}},
            "/home/me/api": {"mcpServers": {"github": {"url": "http://gh/api"}}},
        },
    }
    entries = extract_import_entries(data)
    assert [
        (e.scope, e.group_label, e.original_id, e.proposed_id)
        for e in entries
    ] == [
        ("user", "User scope", "sentry", "sentry"),
        ("project", "web", "github", "web-github"),
        ("project", "api", "github", "api-github"),
    ]
    # Different configs across projects → not flagged as duplicates.
    assert all(e.duplicate_of is None for e in entries)
    assert entries[1].project_path == "/home/me/web"


def test_extract_import_entries_always_prefixes_project_ids() -> None:
    """A project server is prefixed even when its id does not collide."""
    data = {
        "projects": {
            "/x/web": {"mcpServers": {"linear": {"url": "http://l/mcp"}}},
        },
    }
    entries = extract_import_entries(data)
    assert [e.proposed_id for e in entries] == ["web-linear"]


def test_extract_import_entries_numeric_decollide_vs_existing_and_batch() -> None:
    """Numeric suffix only when even the project-prefixed id still
    collides — against existing upstreams and earlier batch entries."""
    data = {
        "mcpServers": {"github": {"url": "http://a/mcp"}},
        "projects": {
            # Same basename "web" via two different paths → same prefix,
            # so the second github needs a numeric bump.
            "/p1/web": {"mcpServers": {"github": {"url": "http://b/mcp"}}},
            "/p2/web": {"mcpServers": {"github": {"url": "http://c/mcp"}}},
        },
    }
    entries = extract_import_entries(data, existing_ids=["github"])
    assert [e.proposed_id for e in entries] == [
        "github-2", "web-github", "web-github-2",
    ]


def test_extract_import_entries_flags_identical_duplicates() -> None:
    """Byte-identical configs across projects are kept (both rows) but
    the later one points at the first via ``duplicate_of``."""
    cfg = {"url": "http://same/mcp"}
    data = {
        "projects": {
            "/p/web": {"mcpServers": {"github": dict(cfg)}},
            "/p/api": {"mcpServers": {"github": dict(cfg)}},
        },
    }
    entries = extract_import_entries(data)
    assert entries[0].duplicate_of is None
    assert entries[1].duplicate_of is not None
    assert entries[1].duplicate_of.proposed_id == "web-github"
    assert entries[1].duplicate_of.group_label == "web"


def test_extract_import_entries_skips_non_server_dicts() -> None:
    """Entries that are not server configs (no url/command, or not a
    dict) are dropped before id assignment."""
    data = {
        "mcpServers": {
            "ok": {"url": "http://x/mcp"},
            "junk": {"foo": 1},
            "bad": 5,
        },
    }
    entries = extract_import_entries(data)
    assert [e.original_id for e in entries] == ["ok"]
