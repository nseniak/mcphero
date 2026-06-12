from __future__ import annotations

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    ToolAccessConfig,
    UserDefinition,
)
from mcpolis.domain.services.policy_engine import PolicyEngine


def make_config(
    roles: dict[str, RoleDefinition] | None = None,
    users: dict[str, UserDefinition] | None = None,
) -> SettingsConfig:
    return SettingsConfig(roles=roles or {}, users=users or {})


# --- Zero-roles org fails closed (no permissive mode) ---
#
# An org with zero configured roles must deny every identity, exactly
# like a role-less user in a roled org. Org membership on /mcp/{slug}
# is enforced by policy, so an allow-all fallback here would open a
# zero-roles org to any authenticated user of the platform.

SAMPLE_TOOLS: list[tuple[str, str, dict[str, bool]]] = [
    ("github", "create_issue", {}),
    ("slack", "send_message", {}),
]


def test_zero_roles_denies_tool_call() -> None:
    engine = PolicyEngine(make_config())
    assert engine.is_empty
    decision = engine.decide_tool_call("anyone", "github", "create_issue", {})
    assert not decision.allowed
    assert decision.reason == "user_not_in_any_role"


def test_zero_roles_allows_no_upstreams() -> None:
    engine = PolicyEngine(make_config())
    assert engine.get_allowed_upstreams("anyone") == set()


def test_zero_roles_filter_tools_returns_nothing() -> None:
    engine = PolicyEngine(make_config())
    assert engine.filter_tools("anyone", SAMPLE_TOOLS) == []


def test_zero_roles_denies_member_with_dangling_role() -> None:
    """A user listed in config.users whose role no longer exists (the
    state an org reaches when its admin deletes every role) fails
    closed on all three entry points."""
    engine = PolicyEngine(
        make_config(users={"member@test.com": UserDefinition(role="ghost")})
    )
    assert engine.is_empty
    assert engine.get_allowed_upstreams("member@test.com") == set()
    assert engine.filter_tools("member@test.com", SAMPLE_TOOLS) == []
    decision = engine.decide_tool_call(
        "member@test.com", "github", "create_issue", {},
    )
    assert not decision.allowed


def test_zero_roles_denies_cross_tenant_user() -> None:
    """User who is a member of org A only, against org B with zero
    roles: no tools, no upstreams. Regression for the permissive-mode
    hole where org B would have been open to any authenticated user."""
    org_a = PolicyEngine(
        make_config(
            roles={
                "dev": RoleDefinition(
                    settings=RoleSettings(
                        mcp_access=McpAccessConfig(mcps={"github": True}),
                    ),
                ),
            },
            users={"alice@test.com": UserDefinition(role="dev")},
        )
    )
    org_b = PolicyEngine(make_config())  # zero roles, alice not a member

    # Sanity: alice has access in her own org.
    assert org_a.get_allowed_upstreams("alice@test.com") == {"github"}

    assert org_b.get_allowed_upstreams("alice@test.com") == set()
    assert org_b.filter_tools("alice@test.com", SAMPLE_TOOLS) == []
    decision = org_b.decide_tool_call(
        "alice@test.com", "github", "create_issue", {},
    )
    assert not decision.allowed
    assert decision.reason == "user_not_in_any_role"


# --- User roles ---


def test_get_user_roles() -> None:
    config = make_config(
        roles={
            "admin": RoleDefinition(is_admin=True),
            "dev": RoleDefinition(),
        },
        users={
            "admin@test.com": UserDefinition(role="admin"),
            "alice@test.com": UserDefinition(role="dev"),
        },
    )
    engine = PolicyEngine(config)
    assert engine.get_user_roles("admin@test.com") == ["admin"]
    assert engine.get_user_roles("alice@test.com") == ["dev"]
    assert engine.get_user_roles("unknown@test.com") == []


def test_has_role() -> None:
    config = make_config(
        roles={"admin": RoleDefinition(is_admin=True)},
        users={"admin@test.com": UserDefinition(role="admin")},
    )
    engine = PolicyEngine(config)
    assert engine.has_role("admin@test.com", "admin")
    assert not engine.has_role("alice@test.com", "admin")


def test_has_role_admin_is_per_org_isolation_gate() -> None:
    """Cross-org isolation regression guard for ``/admin-mcp/{slug}``.

    The ``AdminRoleMiddleware`` in [app.py][1] runs after the slug has
    been resolved to an ``org_id`` and asks
    ``runtime_manager.get(org_id).policy_engine.is_admin(email)``.
    Post-OAuth-state-refactor (commit 725d175) gateway tokens are
    user-scoped and global — they no longer pin a tenant. The ONLY
    thing standing between an authenticated user and another org's
    admin MCP is this ``is_admin`` call returning False when the
    user isn't in the resolved org's ``config.users`` dict.

    A future refactor that, for instance, made ``is_admin`` consult
    a union of memberships across orgs, or that cached resolved
    settings keyed only by email and not by config, would silently
    grant any authenticated user admin access to every org. This
    test pins the invariant: ``is_admin(alice)`` on org B's engine
    MUST be False when alice is admin of org A and absent from
    org B's policy.

    [1]: backend/src/mcpolis/entrypoints/app.py:411 (admin_role_check)
    """
    org_a_config = make_config(
        roles={"admin": RoleDefinition(is_admin=True)},
        users={"alice@example.com": UserDefinition(role="admin")},
    )
    org_b_config = make_config(
        roles={"admin": RoleDefinition(is_admin=True)},
        users={"bob@example.com": UserDefinition(role="admin")},
    )
    engine_a = PolicyEngine(org_a_config)
    engine_b = PolicyEngine(org_b_config)

    # Each user is admin in their own org.
    assert engine_a.is_admin("alice@example.com")
    assert engine_b.is_admin("bob@example.com")

    # The smoking-gun assertions: cross-org admin check rejects.
    assert not engine_b.is_admin("alice@example.com"), (
        "alice (admin of org A) must NOT have admin role in org B; "
        "this is the structural cross-org isolation gate the admin "
        "MCP relies on. See app.py:411 admin_role_check."
    )
    assert not engine_a.is_admin("bob@example.com")


def test_is_admin_routes_through_role_flag_not_role_name() -> None:
    """Same-org regression guard: ``is_admin`` is the canonical
    "is this user an admin" check. It consults
    ``RoleDefinition.is_admin``, NOT a string compare on
    ``user.role``, so a role literally named ``"admin"`` but with
    ``is_admin=False`` is correctly denied — and a role named
    anything else with ``is_admin=True`` is correctly granted.
    Pin both edges. (``has_role`` stays as a strict role-name
    check; admin-ness lives on ``is_admin``.)"""
    config = make_config(
        roles={
            "user": RoleDefinition(is_admin=False),
            "operator": RoleDefinition(is_admin=True),
        },
        users={
            "alice@example.com": UserDefinition(role="user"),
            "bob@example.com": UserDefinition(role="operator"),
        },
    )
    engine = PolicyEngine(config)

    # Non-admin role → False, even though alice is "in" the org.
    assert not engine.is_admin("alice@example.com")
    # Admin-flagged role under a non-"admin" name → True.
    assert engine.is_admin("bob@example.com")


# --- is_admin / admin_role_names / default_admin_role_name ---
#
# These tests pin the contract that "any role flagged is_admin=True
# grants admin", regardless of the role's *name*. They are the
# Phase 0 red tests for the "stop comparing role names with the
# literal 'admin'" refactor: the methods don't exist yet, so the
# tests fail today. They turn green once PolicyEngine grows the
# new helpers.


def test_is_admin_returns_true_for_admin_flagged_role_under_any_name() -> None:
    """``is_admin`` is the canonical "is this user an admin" check.
    A role flagged ``is_admin=True`` grants admin even when its name
    is something other than ``"admin"`` — and a role *named* "admin"
    with ``is_admin=False`` does NOT grant admin. The literal-string
    comparison this method replaces silently violates both edges."""
    config = make_config(
        roles={
            # Same name as the historical default, but no admin flag.
            "admin": RoleDefinition(is_admin=False),
            # Admin flag under a different name.
            "operator": RoleDefinition(is_admin=True),
        },
        users={
            "alice@example.com": UserDefinition(role="admin"),
            "bob@example.com": UserDefinition(role="operator"),
        },
    )
    engine = PolicyEngine(config)

    # The operator is admin (flag wins over name).
    assert engine.is_admin("bob@example.com")
    # The "admin"-named-but-flag-False user is NOT admin.
    assert not engine.is_admin("alice@example.com")
    # Unknown user → False.
    assert not engine.is_admin("nobody@example.com")


def test_admin_role_names_returns_set_of_admin_flagged_role_names() -> None:
    """``admin_role_names()`` enumerates the admin roles in this
    org's config. Used by callers that need to filter memberships /
    users by admin-ness without round-tripping through every user
    via ``is_admin``."""
    config = make_config(
        roles={
            "admin": RoleDefinition(is_admin=False),
            "operator": RoleDefinition(is_admin=True),
            "viewer": RoleDefinition(is_admin=False),
            "owner": RoleDefinition(is_admin=True),
        },
    )
    engine = PolicyEngine(config)
    assert engine.admin_role_names() == {"operator", "owner"}


def test_default_admin_role_name_resolves_seed_role() -> None:
    """``default_admin_role_name()`` is the seed-time helper used by
    ``OrgService.create_organization`` (and friends) to assign the
    creator to "the role flagged is_admin=True" without baking in
    the literal name ``"admin"``."""
    config = make_config(
        roles={
            "operator": RoleDefinition(is_admin=True),
            "viewer": RoleDefinition(is_admin=False),
        },
    )
    engine = PolicyEngine(config)
    assert engine.default_admin_role_name() == "operator"


# --- User not in any role (default deny) ---


def test_unknown_user_denied() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"github": True}),
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    decision = engine.decide_tool_call("unknown@test.com", "github", "create_issue", {})
    assert not decision.allowed
    assert decision.reason == "user_not_in_any_role"


def test_unknown_user_gets_no_upstreams() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(mcp_access=McpAccessConfig(mcps={"github": True})),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    assert engine.get_allowed_upstreams("unknown@test.com") == set()


def test_unknown_user_sees_no_tools() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"github": True}),
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    tools: list[tuple[str, str, dict[str, bool]]] = [("github", "create_issue", {})]
    assert engine.filter_tools("unknown@test.com", tools) == []


# --- Explicit override grants access ---


def test_explicit_override_grants_access() -> None:
    """Only explicit per-MCP entries determine runtime access."""
    config = make_config(
        roles={"admin": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(
                    mcps={"github": True, "slack": True},
                ),
            ),
        )},
        users={"admin@test.com": UserDefinition(role="admin")},
    )
    engine = PolicyEngine(config)
    assert engine.get_allowed_upstreams("admin@test.com") == {"github", "slack"}
    decision = engine.decide_tool_call("admin@test.com", "github", "any_tool", {})
    assert decision.allowed
    decision = engine.decide_tool_call("admin@test.com", "jira", "any_tool", {})
    assert not decision.allowed


# --- Specific MCP access ---


def test_specific_mcp_access() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(mcp_access=McpAccessConfig(mcps={"github": True})),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    assert engine.get_allowed_upstreams("alice@test.com") == {"github"}

    decision = engine.decide_tool_call("alice@test.com", "github", "create_issue", {})
    assert decision.allowed

    decision = engine.decide_tool_call("alice@test.com", "slack", "send_message", {})
    assert not decision.allowed


# --- Tool-level allow/deny ---


def test_tool_level_allow() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"slack": True}),
                tool_access={"slack": ToolAccessConfig(
                    fallback_enabled=False,
                    tools={"read_messages": True, "list_channels": True},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)

    decision = engine.decide_tool_call("alice@test.com", "slack", "read_messages", {})
    assert decision.allowed

    decision = engine.decide_tool_call("alice@test.com", "slack", "send_message", {})
    assert not decision.allowed


def test_tool_level_deny_overrides_allow() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"slack": True}),
                tool_access={"slack": ToolAccessConfig(
                    fallback_enabled=True,
                    tools={"send_message": False},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)

    decision = engine.decide_tool_call("alice@test.com", "slack", "read_messages", {})
    assert decision.allowed

    decision = engine.decide_tool_call("alice@test.com", "slack", "send_message", {})
    assert not decision.allowed


def test_filter_tools_with_allow_and_deny() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"slack": True}),
                tool_access={"slack": ToolAccessConfig(
                    fallback_enabled=True,
                    tools={"send_message": False},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    tools: list[tuple[str, str, dict[str, bool]]] = [
        ("slack", "read_messages", {}),
        ("slack", "send_message", {}),
        ("slack", "list_channels", {}),
    ]
    result = engine.filter_tools("alice@test.com", tools)
    result_pairs = [(uid, name) for uid, name, _ in result]
    assert ("slack", "read_messages") in result_pairs
    assert ("slack", "list_channels") in result_pairs
    assert ("slack", "send_message") not in result_pairs


# --- Argument validation ---


def test_argument_allow_pattern_match() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"database": True}),
                tool_access={"database": ToolAccessConfig(fallback_enabled=False, tools={"query": True})},
                argument_constraints={
                    "database__query": {
                        "sql": ArgumentConstraint(pattern=r"^SELECT\s", mode="allow"),
                    },
                },
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)

    decision = engine.decide_tool_call(
        "alice@test.com", "database", "query", {"sql": "SELECT * FROM users"}
    )
    assert decision.allowed

    decision = engine.decide_tool_call(
        "alice@test.com", "database", "query", {"sql": "INSERT INTO users VALUES (1)"}
    )
    assert not decision.allowed
    assert "pattern" in decision.reason


def test_argument_forbid_pattern_match() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"database": True}),
                tool_access={"database": ToolAccessConfig(fallback_enabled=False, tools={"query": True})},
                argument_constraints={
                    "database__query": {
                        "sql": ArgumentConstraint(pattern=r"DROP|DELETE|TRUNCATE", mode="forbid"),
                    },
                },
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)

    decision = engine.decide_tool_call(
        "alice@test.com", "database", "query", {"sql": "SELECT * FROM users"}
    )
    assert decision.allowed

    decision = engine.decide_tool_call(
        "alice@test.com", "database", "query", {"sql": "DROP TABLE users"}
    )
    assert not decision.allowed
    assert "forbidden" in decision.reason

    # Forbid mode is case-insensitive
    decision = engine.decide_tool_call(
        "alice@test.com", "database", "query", {"sql": "drop table users"}
    )
    assert not decision.allowed


def test_argument_missing_does_not_fail() -> None:
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"database": True}),
                tool_access={"database": ToolAccessConfig(fallback_enabled=False, tools={"query": True})},
                argument_constraints={
                    "database__query": {
                        "sql": ArgumentConstraint(pattern=r"^SELECT", mode="allow"),
                    },
                },
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)

    decision = engine.decide_tool_call(
        "alice@test.com", "database", "query", {"other_arg": "value"}
    )
    assert decision.allowed


def test_reload_updates_config() -> None:
    config1 = make_config(
        roles={
            "admin": RoleDefinition(
                is_admin=True,
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"github": True, "slack": True}),
                ),
            ),
        },
        users={"admin@test.com": UserDefinition(role="admin")},
    )
    engine = PolicyEngine(config1)
    assert engine.get_allowed_upstreams("admin@test.com") == {"github", "slack"}

    config2 = make_config(
        roles={
            "admin": RoleDefinition(
                is_admin=True,
                settings=RoleSettings(mcp_access=McpAccessConfig(mcps={"github": True})),
            ),
        },
        users={"admin@test.com": UserDefinition(role="admin")},
    )
    engine.reload(config2)
    assert engine.get_allowed_upstreams("admin@test.com") == {"github"}


# --- Annotation-based tool access ---


def test_category_default_allows_read_only() -> None:
    """Category defaults allow read-only tools when configured."""
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"slack": True}),
                tool_access={"slack": ToolAccessConfig(
                    fallback_enabled=False,
                    category_defaults={"readOnly": True},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    # Read-only tool allowed
    decision = engine.decide_tool_call(
        "alice@test.com", "slack", "list_channels", {},
        tool_annotations={"readOnly": True},
    )
    assert decision.allowed
    # Non-read-only tool denied (falls to fallback_enabled=False)
    decision = engine.decide_tool_call(
        "alice@test.com", "slack", "send_message", {},
        tool_annotations={},
    )
    assert not decision.allowed


def test_annotation_deny_wins() -> None:
    """When a tool matches multiple category defaults, deny wins."""
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"db": True}),
                tool_access={"db": ToolAccessConfig(
                    fallback_enabled=True,
                    category_defaults={"readOnly": True, "destructive": False},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    # Tool that is both read-only and destructive — deny wins
    decision = engine.decide_tool_call(
        "alice@test.com", "db", "drop_and_report", {},
        tool_annotations={"readOnly": True, "destructive": True},
    )
    assert not decision.allowed


def test_category_default_beats_explicit_override() -> None:
    """Category defaults take precedence over explicit per-tool overrides."""
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"db": True}),
                tool_access={"db": ToolAccessConfig(
                    category_defaults={"destructive": False},
                    tools={"special_delete": True},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    # Destructive category default (False) wins over explicit override (True)
    decision = engine.decide_tool_call(
        "alice@test.com", "db", "special_delete", {},
        tool_annotations={"destructive": True},
    )
    assert not decision.allowed
    # Non-destructive tool with explicit override IS allowed
    decision = engine.decide_tool_call(
        "alice@test.com", "db", "special_delete", {},
        tool_annotations={},
    )
    assert decision.allowed


def test_no_tool_access_config_allows_all() -> None:
    """When no ToolAccessConfig exists for an upstream, all tools are allowed."""
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(mcp_access=McpAccessConfig(mcps={"slack": True})),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    decision = engine.decide_tool_call(
        "alice@test.com", "slack", "any_tool", {},
        tool_annotations={"destructive": True},
    )
    assert decision.allowed


def test_per_tool_mode_denies_unknown_tools() -> None:
    """In per-tool mode (fallback_enabled=None), unknown tools are denied."""
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"slack": True}),
                tool_access={"slack": ToolAccessConfig(
                    tools={"known_tool": True},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    # Known tool is allowed
    decision = engine.decide_tool_call(
        "alice@test.com", "slack", "known_tool", {},
    )
    assert decision.allowed
    # Unknown tool is denied
    decision = engine.decide_tool_call(
        "alice@test.com", "slack", "new_unknown_tool", {},
    )
    assert not decision.allowed


def test_filter_tools_with_annotations() -> None:
    """filter_tools respects category defaults."""
    config = make_config(
        roles={"dev": RoleDefinition(
            settings=RoleSettings(
                mcp_access=McpAccessConfig(mcps={"db": True}),
                tool_access={"db": ToolAccessConfig(
                    fallback_enabled=False,
                    category_defaults={"readOnly": True},
                )},
            ),
        )},
        users={"alice@test.com": UserDefinition(role="dev")},
    )
    engine = PolicyEngine(config)
    tools: list[tuple[str, str, dict[str, bool]]] = [
        ("db", "query", {"readOnly": True}),
        ("db", "drop_table", {"destructive": True}),
        ("db", "list_tables", {"readOnly": True}),
    ]
    result = engine.filter_tools("alice@test.com", tools)
    result_names = [name for _, name, _ in result]
    assert "query" in result_names
    assert "list_tables" in result_names
    assert "drop_table" not in result_names


# --- Boundary role (service tokens) ---


def make_boundary_config() -> SettingsConfig:
    """Two roles, no svc identity in users (by design)."""
    return make_config(
        roles={
            "user": RoleDefinition(
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(
                        mcps={"github": True, "db": True},
                    ),
                ),
            ),
            "reader": RoleDefinition(
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"db": True}),
                    tool_access={"db": ToolAccessConfig(
                        fallback_enabled=False,
                        category_defaults={"readOnly": True},
                    )},
                ),
            ),
        },
        users={"alice@test.com": UserDefinition(role="user")},
    )


def test_boundary_role_filters_tools_without_users_entry() -> None:
    engine = PolicyEngine(make_boundary_config())
    tools: list[tuple[str, str, dict[str, bool]]] = [
        ("github", "create_issue", {}),
        ("db", "query", {"readOnly": True}),
    ]
    # svc identity is NOT in config.users; the boundary role drives.
    result = engine.filter_tools(
        "svc:ci-bot", tools, boundary_role="user",
    )
    assert result == tools
    # Without the boundary role the same identity gets nothing.
    assert engine.filter_tools("svc:ci-bot", tools) == []


def test_boundary_role_decide_tool_call_allows_and_denies_per_role() -> None:
    engine = PolicyEngine(make_boundary_config())
    allowed = engine.decide_tool_call(
        "svc:ci-bot", "db", "query", {},
        tool_annotations={"readOnly": True},
        boundary_role="reader",
    )
    assert allowed.allowed
    assert allowed.matched_role == "reader"

    denied_tool = engine.decide_tool_call(
        "svc:ci-bot", "db", "drop_table", {},
        tool_annotations={"destructive": True},
        boundary_role="reader",
    )
    assert not denied_tool.allowed

    denied_mcp = engine.decide_tool_call(
        "svc:ci-bot", "github", "create_issue", {},
        boundary_role="reader",
    )
    assert not denied_mcp.allowed
    assert "not allowed" in denied_mcp.reason


def test_boundary_role_deleted_role_fails_closed() -> None:
    engine = PolicyEngine(make_boundary_config())
    assert engine.get_allowed_upstreams(
        "svc:ci-bot", boundary_role="deleted-role",
    ) == set()
    decision = engine.decide_tool_call(
        "svc:ci-bot", "db", "query", {}, boundary_role="deleted-role",
    )
    assert not decision.allowed
    assert decision.reason == "user_not_in_any_role"


def test_boundary_role_none_keeps_email_lookup_behavior() -> None:
    engine = PolicyEngine(make_boundary_config())
    upstreams = engine.get_allowed_upstreams("alice@test.com")
    assert upstreams == {"github", "db"}
    # Explicit None is the same as omitting the kwarg.
    assert engine.get_allowed_upstreams(
        "alice@test.com", boundary_role=None,
    ) == {"github", "db"}


def test_boundary_role_get_allowed_upstreams() -> None:
    engine = PolicyEngine(make_boundary_config())
    assert engine.get_allowed_upstreams(
        "svc:ci-bot", boundary_role="reader",
    ) == {"db"}


def test_boundary_role_zero_roles_org_fails_closed() -> None:
    """A service token's access is exactly its minted role, in every
    org state — a zero-roles org denies it on all three entry points
    (humans fail closed too; see the zero-roles section above)."""
    engine = PolicyEngine(make_config())  # no roles, no users
    assert engine.is_empty
    assert engine.get_allowed_upstreams(
        "svc:bot", boundary_role="ghost-role",
    ) == set()
    tools: list[tuple[str, str, dict[str, bool]]] = [("db", "query", {})]
    assert engine.filter_tools(
        "svc:bot", tools, boundary_role="ghost-role",
    ) == []
    decision = engine.decide_tool_call(
        "svc:bot", "db", "query", {}, boundary_role="ghost-role",
    )
    assert not decision.allowed
    assert decision.reason == "user_not_in_any_role"
