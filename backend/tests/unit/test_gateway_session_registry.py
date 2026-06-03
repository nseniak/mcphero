from __future__ import annotations

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.domain.model.audit import parse_client_type
from mcpolis.domain.ports import DEFAULT_ORG_ID


def test_register_and_lookup() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.register("s2", DEFAULT_ORG_ID, "alice")
    reg.register("s3", DEFAULT_ORG_ID, "bob")

    assert sorted(
        reg.get_session_ids_for_user(DEFAULT_ORG_ID, "alice")
    ) == ["s1", "s2"]
    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "bob") == ["s3"]
    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "unknown") == []


def test_unregister() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.register("s2", DEFAULT_ORG_ID, "alice")
    reg.unregister("s1")

    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "alice") == ["s2"]
    # Unregistering again is a no-op
    reg.unregister("s1")
    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "alice") == ["s2"]


def test_unregister_last_session_cleans_user() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.unregister("s1")
    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "alice") == []
    assert reg.get_all_session_ids() == []


def test_re_register_updates_user() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.register("s1", DEFAULT_ORG_ID, "bob")

    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "alice") == []
    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "bob") == ["s1"]


def test_get_all_session_ids() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.register("s2", DEFAULT_ORG_ID, "bob")
    assert sorted(reg.get_all_session_ids()) == ["s1", "s2"]


def test_idempotent_register() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    assert reg.get_session_ids_for_user(DEFAULT_ORG_ID, "alice") == ["s1"]


def test_has_session() -> None:
    reg = GatewaySessionRegistry()
    assert not reg.has_session("s1")
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    assert reg.has_session("s1")
    reg.unregister("s1")
    assert not reg.has_session("s1")


def test_on_disconnect_callback() -> None:
    events: list[tuple[str, str, str]] = []
    reg = GatewaySessionRegistry()
    reg.set_on_disconnect(
        lambda sid, oid, uid: events.append((sid, oid, uid))
    )
    reg.register("s1", DEFAULT_ORG_ID, "alice")
    reg.unregister("s1")
    assert events == [("s1", DEFAULT_ORG_ID, "alice")]
    # Unregistering unknown session does not fire callback
    reg.unregister("s1")
    assert events == [("s1", DEFAULT_ORG_ID, "alice")]


def test_register_different_orgs_same_user() -> None:
    """Phase 2a: same user_id in different orgs is tracked separately."""
    reg = GatewaySessionRegistry()
    reg.register("s1", "acme", "alice")
    reg.register("s2", "beta", "alice")
    assert reg.get_session_ids_for_user("acme", "alice") == ["s1"]
    assert reg.get_session_ids_for_user("beta", "alice") == ["s2"]


def test_get_session_owner() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", "acme", "alice")
    assert reg.get_session_owner("s1") == ("acme", "alice")
    assert reg.get_session_owner("unknown") is None


def test_get_session_ids_for_org_scopes_by_org() -> None:
    reg = GatewaySessionRegistry()
    reg.register("s1", "acme", "alice")
    reg.register("s2", "acme", "bob")
    reg.register("s3", "beta", "alice")
    assert sorted(reg.get_session_ids_for_org("acme")) == ["s1", "s2"]
    assert reg.get_session_ids_for_org("beta") == ["s3"]
    assert reg.get_session_ids_for_org("unknown") == []


def test_get_session_ids_for_user_includes_multi_org_sentinel_sessions() -> None:
    """A user connected via the cloud ``/mcp`` endpoint registers under
    ``MULTI_ORG_SENTINEL`` (no slug in the URL). When a role-scoped
    policy change targets one of their member-orgs, the sentinel-bound
    session must still surface — otherwise role mutations never reach
    the cloud gateway client."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    reg = GatewaySessionRegistry()
    # Cloud /mcp session for alice — registered with the sentinel.
    reg.register("s-cloud", MULTI_ORG_SENTINEL, "alice")
    # Slug-scoped admin MCP session for the same user in acme.
    reg.register("s-admin", "acme", "alice")
    # Lookup against the *real* org should pick up BOTH sessions —
    # the slug-bound one explicitly, the sentinel one via the sweep.
    assert sorted(
        reg.get_session_ids_for_user("acme", "alice"),
    ) == ["s-admin", "s-cloud"]


def test_get_session_ids_for_user_under_sentinel_does_not_double_count() -> None:
    """Looking up by the sentinel itself should not duplicate sessions —
    the cross-org sweep is one-directional (real org → sentinel)."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    reg = GatewaySessionRegistry()
    reg.register("s-cloud", MULTI_ORG_SENTINEL, "alice")
    assert reg.get_session_ids_for_user(
        MULTI_ORG_SENTINEL, "alice",
    ) == ["s-cloud"]


def test_get_session_ids_for_org_includes_sentinel_for_user_set() -> None:
    """``get_session_ids_for_org`` widens to multi-org sentinel sessions
    when given a member set. Without the widening, an upstream-side
    ``tools/list_changed`` for org A never reaches a cloud-gateway
    user who has A in their org list."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    reg = GatewaySessionRegistry()
    reg.register("s-admin", "acme", "alice")
    reg.register("s-cloud-alice", MULTI_ORG_SENTINEL, "alice")
    reg.register("s-cloud-bob", MULTI_ORG_SENTINEL, "bob")  # not in acme

    out = reg.get_session_ids_for_org(
        "acme", user_ids_in_org={"alice"},
    )
    assert sorted(out) == ["s-admin", "s-cloud-alice"]
    # Without the user set, sentinel-bound sessions are not swept in
    # (preserves the previous narrow contract for callers that
    # haven't migrated yet).
    out_strict = reg.get_session_ids_for_org("acme")
    assert out_strict == ["s-admin"]


def test_parse_client_type_claude_code() -> None:
    assert parse_client_type("ClaudeCode/1.0") == "Claude Code"


def test_parse_client_type_claude_desktop() -> None:
    assert parse_client_type("ClaudeDesktop/2.0 (macOS)") == "Claude Desktop"


def test_parse_client_type_cursor() -> None:
    assert parse_client_type("Cursor/0.50.1") == "Cursor"


def test_parse_client_type_vscode() -> None:
    assert parse_client_type("VS Code/1.95.0") == "VS Code"
    assert parse_client_type("Visual Studio Code/1.95") == "VS Code"


def test_parse_client_type_jetbrains() -> None:
    assert parse_client_type("IntelliJ/2024.3") == "JetBrains"
    assert parse_client_type("PyCharm/2024.3") == "JetBrains"


def test_parse_client_type_unknown_with_slash() -> None:
    # Unknown UA with slash: return first segment
    assert parse_client_type("SomeClient/1.0") == "SomeClient"


def test_parse_client_type_unknown_without_slash() -> None:
    assert parse_client_type("randomstring") == "Unknown"


def test_parse_client_type_none() -> None:
    assert parse_client_type(None) == "Unknown"
    assert parse_client_type("") == "Unknown"
