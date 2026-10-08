"""Each org's runtime carries its own roles lock (see ``OrgRuntime``)."""
from __future__ import annotations

from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.services.org_runtime import OrgRuntime
from mcpolis.domain.services.policy_engine import PolicyEngine
from tests.unit.factories import make_runtime_manager


def make_runtime(org_id: str) -> OrgRuntime:
    manager = make_runtime_manager(PolicyEngine(SettingsConfig()), org_id=org_id)
    runtime = manager.get_cached(org_id)
    assert runtime is not None
    return runtime


def test_each_org_runtime_has_its_own_roles_lock() -> None:
    """Role changes in one org must not wait on another org's."""
    org_a = make_runtime("org-a")
    org_b = make_runtime("org-b")
    assert org_a.roles_lock is not org_b.roles_lock
    # The same lock on every read, or holding it would guard nothing.
    assert org_a.roles_lock is org_a.roles_lock
