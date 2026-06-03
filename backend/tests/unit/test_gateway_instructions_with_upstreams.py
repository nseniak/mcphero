"""Self-description folding into the gateway's ``initialize`` instructions.

Each upstream that advertises ``serverInfo.description`` /
``instructions`` at connect time should have a one-line summary appear
in the gateway's downstream ``instructions`` text — single-org and
multi-org variants. Long descriptions are truncated at a clean word
boundary so a single chatty upstream can't dominate the text.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
    UpstreamSelfDescription,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.organization_repository import Organization
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.entrypoints.controllers.gateway_controller import (
    UPSTREAM_DESCRIPTION_TRUNCATION_CHARS,
    _instructions_for_org_with_upstreams,
    _instructions_with_upstreams_multi_org,
    _truncate_at_word_boundary,
)


# ─── Builders ──────────────────────────────────────────────────────────


def make_upstream(
    upstream_id: str, *, display_name: str | None = None,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id,
        display_name=display_name or upstream_id.title(),
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=f"http://upstream.invalid/{upstream_id}"),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


def make_runtime(
    org_id: str,
    upstreams_with_descriptions: list[
        tuple[str, str | None, UpstreamSelfDescription | None]
    ],
) -> OrgRuntime:
    """Build a runtime + seed self-descriptions on its client manager."""
    upstreams = [
        make_upstream(uid, display_name=name)
        for uid, name, _ in upstreams_with_descriptions
    ]
    cm = UpstreamClientManager(upstreams)
    from tests.unit._state_seed import seed_self_description
    for uid, _, sd in upstreams_with_descriptions:
        if sd is not None:
            seed_self_description(cm, uid, sd)
    registry = ToolRegistry(upstreams, cm)
    return OrgRuntime(
        org_id=org_id,
        policy_engine=PolicyEngine(SettingsConfig()),
        tool_registry=registry,
        client_manager=cm,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=upstreams,
    )


def make_manager(
    runtimes: dict[str, OrgRuntime],
) -> OrgRuntimeManager:
    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    for org_id, rt in runtimes.items():
        manager._runtimes[org_id] = rt  # pyright: ignore[reportPrivateUsage]
        manager._startup_status[org_id] = MagicMock(  # pyright: ignore[reportPrivateUsage]
            ready=True, total=0, connected=set(), failed=set(),
        )
    return manager


def make_org(slug: str, display_name: str) -> Organization:
    return Organization(
        id=f"{slug}-id", slug=slug, display_name=display_name,
        created_at=datetime.now(UTC), created_by_email="creator@test.com",
    )


# ─── Tests ──────────────────────────────────────────────────────────────


def test_single_org_appends_description_for_each_upstream() -> None:
    runtime = make_runtime(
        DEFAULT_ORG_ID,
        [
            (
                "notion", "Notion",
                UpstreamSelfDescription(
                    name="notion-mcp", version="0.4.2",
                    description="Search and read pages.",
                ),
            ),
        ],
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    out = _instructions_for_org_with_upstreams(
        rm, DEFAULT_ORG_ID,
        base_instructions="BASE",
        name_prefix="",
    )
    assert "BASE" in out
    assert "Connected upstreams:" in out
    assert "notion (Notion): Search and read pages." in out


def test_single_org_omits_upstream_with_no_description() -> None:
    runtime = make_runtime(
        DEFAULT_ORG_ID,
        [
            (
                "notion", "Notion",
                UpstreamSelfDescription(
                    name="notion-mcp", version="0.4.2",
                    description="Search and read pages.",
                ),
            ),
            ("mixpanel", "Mixpanel", None),  # no self-description recorded
            (
                "blank", "Blank",
                UpstreamSelfDescription(
                    name="blank", version="1.0",
                ),  # has SD but no description / instructions
            ),
        ],
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    out = _instructions_for_org_with_upstreams(
        rm, DEFAULT_ORG_ID,
        base_instructions="BASE",
        name_prefix="",
    )
    assert "notion" in out
    assert "mixpanel" not in out
    assert "blank" not in out


def test_single_org_returns_base_when_no_descriptions() -> None:
    runtime = make_runtime(
        DEFAULT_ORG_ID,
        [("mixpanel", "Mixpanel", None)],
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    out = _instructions_for_org_with_upstreams(
        rm, DEFAULT_ORG_ID,
        base_instructions="BASE",
        name_prefix="",
    )
    assert out == "BASE"


def test_truncate_at_word_boundary_keeps_short_strings() -> None:
    short = "tiny"
    assert _truncate_at_word_boundary(short, cap=100) == short


def test_truncate_at_word_boundary_truncates_long_strings_cleanly() -> None:
    """Truncation must (a) cap the length, (b) prefer a word boundary,
    (c) suffix an ellipsis. The truncated output should not start a
    word mid-character."""
    # Build a string of words separated by spaces so a word boundary exists.
    words = [f"word{i}" for i in range(200)]
    src = " ".join(words)
    assert len(src) > UPSTREAM_DESCRIPTION_TRUNCATION_CHARS
    out = _truncate_at_word_boundary(
        src, cap=UPSTREAM_DESCRIPTION_TRUNCATION_CHARS,
    )
    assert len(out) <= UPSTREAM_DESCRIPTION_TRUNCATION_CHARS + 1  # ellipsis
    assert out.endswith("…")
    assert " " not in out[-2:-1]  # last char before ellipsis isn't a space


def test_single_org_truncates_long_descriptions() -> None:
    long = "x" * 2000
    runtime = make_runtime(
        DEFAULT_ORG_ID,
        [
            (
                "notion", "Notion",
                UpstreamSelfDescription(
                    name="notion-mcp", version="0.4.2",
                    description=long,
                ),
            ),
        ],
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    out = _instructions_for_org_with_upstreams(
        rm, DEFAULT_ORG_ID,
        base_instructions="BASE",
        name_prefix="",
    )
    # Ellipsis suffixed when truncated.
    assert "…" in out
    # The full 2000-char string must NOT have made it through.
    assert long not in out


def test_multi_org_groups_each_org_with_slug_prefix() -> None:
    org_a = make_org("acme", "Acme")
    org_b = make_org("beta", "Beta")
    runtime_a = make_runtime(
        org_a.id,
        [
            (
                "notion", "Notion",
                UpstreamSelfDescription(
                    name="notion-mcp", version="0.4.2",
                    description="Search Notion pages.",
                ),
            ),
        ],
    )
    runtime_b = make_runtime(
        org_b.id,
        [
            (
                "slack", "Slack",
                UpstreamSelfDescription(
                    name="slack-mcp", version="1.0",
                    description="Send Slack messages.",
                ),
            ),
        ],
    )
    rm = make_manager({org_a.id: runtime_a, org_b.id: runtime_b})
    out = _instructions_with_upstreams_multi_org(
        rm, [org_a, org_b], base_instructions="BASE",
    )
    assert "BASE" in out
    # Slug + upstream id per line.
    assert "acme__notion (Notion): Search Notion pages." in out
    assert "beta__slack (Slack): Send Slack messages." in out


def test_multi_org_returns_base_when_no_orgs_have_descriptions() -> None:
    org_a = make_org("acme", "Acme")
    runtime_a = make_runtime(org_a.id, [("notion", "Notion", None)])
    rm = make_manager({org_a.id: runtime_a})
    out = _instructions_with_upstreams_multi_org(
        rm, [org_a], base_instructions="BASE",
    )
    assert out == "BASE"


def test_instructions_falls_back_to_multi_org_default_when_user_orgs_none() -> None:
    """Test isolation guard: if the per-request ``current_user_orgs`` was
    never set, the gateway must fall back to ``_MULTI_ORG_INSTRUCTIONS``
    not blow up. We exercise the wiring by reading the default value.
    """
    from mcpolis.entrypoints.controllers.gateway_controller import (
        _MULTI_ORG_INSTRUCTIONS,
        current_user_orgs,
    )
    assert current_user_orgs.get() is None
    assert _MULTI_ORG_INSTRUCTIONS  # non-empty fallback


def test_picks_up_upstreams_added_after_runtime_construction() -> None:
    """``runtime.upstreams`` is frozen at construction time, but the
    registry is updated by ``register_upstream`` whenever the dashboard
    API adds a new upstream. The instructions block must follow the
    registry, not the frozen list — otherwise upstreams added at
    runtime never have their description folded in.
    """
    # Build a runtime with NO upstreams in the frozen list…
    runtime = make_runtime(DEFAULT_ORG_ID, [])
    # …then dynamically register one (mirroring
    # ``UpstreamConfigService.add_upstream``).
    upstream = make_upstream("notion", display_name="Notion")
    runtime.tool_registry.register_upstream(upstream)
    runtime.client_manager.register_upstream(upstream)
    from tests.unit._state_seed import seed_self_description
    seed_self_description(
        runtime.client_manager, "notion",
        UpstreamSelfDescription(
            name="notion-mcp", version="0.4.2",
            description="Search Notion pages.",
        ),
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    out = _instructions_for_org_with_upstreams(
        rm, DEFAULT_ORG_ID,
        base_instructions="BASE",
        name_prefix="",
    )
    assert "Search Notion pages." in out


def test_uses_instructions_field_when_description_absent() -> None:
    """Some upstreams send only ``instructions`` (top-level) without
    ``serverInfo.description``. Either field is good content — fall
    through to ``instructions`` when ``description`` is None."""
    runtime = make_runtime(
        DEFAULT_ORG_ID,
        [
            (
                "notion", "Notion",
                UpstreamSelfDescription(
                    name="notion-mcp", version="0.4.2",
                    instructions="Use this MCP wisely.",
                ),
            ),
        ],
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    out = _instructions_for_org_with_upstreams(
        rm, DEFAULT_ORG_ID,
        base_instructions="BASE",
        name_prefix="",
    )
    assert "Use this MCP wisely." in out


@pytest.mark.asyncio
async def test_init_options_pull_in_upstream_descriptions_for_single_org() -> None:
    """End-to-end: the actual ``create_initialization_options`` path on
    the gateway server must include the upstream descriptions."""
    from mcpolis.entrypoints.controllers.gateway_controller import (
        create_mcp_server,
        current_org_id,
    )

    runtime = make_runtime(
        DEFAULT_ORG_ID,
        [
            (
                "notion", "Notion",
                UpstreamSelfDescription(
                    name="notion-mcp", version="0.4.2",
                    description="Search Notion pages.",
                ),
            ),
        ],
    )
    rm = make_manager({DEFAULT_ORG_ID: runtime})
    rm.register_display_name(DEFAULT_ORG_ID, "Default")
    rm.register_slug(DEFAULT_ORG_ID, DEFAULT_ORG_ID)

    server = create_mcp_server(rm)
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    try:
        opts = server.create_initialization_options()
    finally:
        current_org_id.reset(org_token)

    assert opts.instructions is not None
    assert "Search Notion pages." in opts.instructions
