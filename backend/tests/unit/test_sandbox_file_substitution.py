"""Substitution tests for system / user Variables and target_path.

After dropping ``${FILE_NAME}`` substitution, the rules collapse to:

1. ``${...}`` resolves against (system + user) Variables — same set
   everywhere the substitutor runs (env values, command, args, url,
   headers, AND ``target_path``).
2. ``${HOME}`` is the canonical system Variable.
3. User Variables are scoped per-upstream and never recurse: a user
   var that resolves to a literal containing ``${B}`` stays as the
   literal text ``${B}`` after one substitution pass.
4. Cycles are structurally impossible: files don't export symbols,
   so user vars and target_paths can both reference (system + user)
   vars but neither can reference a file.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.model.template_var import MissingTemplateVarError
from mcpolis.domain.services.system_variables import (
    DEFAULT_SANDBOX_HOME,
    is_system_variable_name,
    system_variable_names,
    system_variables_for_sandbox,
)
from mcpolis.domain.services.template_var_substitution import (
    make_layered_resolver,
    substitute_string,
)


def test_system_variables_include_home() -> None:
    sv = system_variables_for_sandbox()
    assert sv["HOME"] == DEFAULT_SANDBOX_HOME


def test_system_variables_home_is_provider_overridable() -> None:
    """``${HOME}`` resolves to the provider's home, not a hardcoded
    constant. The manager passes the active provider's
    ``sandbox_home`` so substitution matches the spawned process's
    real ``$HOME`` (e.g. a local-subprocess per-session temp dir)."""
    sv = system_variables_for_sandbox("/tmp/mcpolis-local-home-abc123")
    assert sv["HOME"] == "/tmp/mcpolis-local-home-abc123"


def test_system_variable_names_reserved() -> None:
    assert "HOME" in system_variable_names()
    assert is_system_variable_name("HOME")
    assert not is_system_variable_name("GITHUB_TOKEN")


def test_substitute_home_in_target_path_value_and_args() -> None:
    resolver = make_layered_resolver({"HOME": "/home/user"})
    assert (
        substitute_string(
            "${HOME}/.config/gcloud/credentials.json",
            resolver=resolver,
            upstream_id="up",
        )
        == "/home/user/.config/gcloud/credentials.json"
    )


def test_layered_resolver_prefers_earlier_layers() -> None:
    """System Variables (earlier layer) shadow user Variables.

    The write-side rejects user vars named after a system Variable,
    so this case shouldn't arise in practice — but explicit
    precedence here keeps the contract independent of write-time
    enforcement.
    """
    layered = make_layered_resolver(
        {"HOME": "/home/user"},  # system
        {"GITHUB_TOKEN": "ghp_x"},  # user
    )
    assert layered("HOME") == "/home/user"
    assert layered("GITHUB_TOKEN") == "ghp_x"
    assert layered("UNKNOWN") is None


def test_target_path_renders_with_user_vars() -> None:
    """``target_path`` accepts the same ``${...}`` references env-var
    values accept. End-to-end of a per-tenant recipe: the file's
    target_path mixes ``${HOME}`` (system) and ``${TENANT_ID}`` (user).
    """
    resolver = make_layered_resolver(
        {"HOME": "/home/user"},
        {"TENANT_ID": "acme"},
    )
    rendered = substitute_string(
        "${HOME}/.config/myapp/${TENANT_ID}/creds.json",
        resolver=resolver,
        upstream_id="up",
    )
    assert rendered == "/home/user/.config/myapp/acme/creds.json"


def test_gcp_recipe_uses_literal_paths_on_both_sides() -> None:
    """The GCP recipe after dropping ``${FILE_NAME}``: operator types
    the same ``${HOME}/...`` literal in BOTH the file's target_path
    and the Variable value. Both render to the same absolute path,
    no exported symbol needed.
    """
    resolver = make_layered_resolver({"HOME": "/home/user"})
    file_target_path = substitute_string(
        "${HOME}/.config/gcloud/credentials.json",
        resolver=resolver,
        upstream_id="up",
    )
    env_var_value = substitute_string(
        "${HOME}/.config/gcloud/credentials.json",
        resolver=resolver,
        upstream_id="up",
    )
    assert file_target_path == env_var_value
    assert file_target_path == "/home/user/.config/gcloud/credentials.json"


def test_missing_reference_raises_clear_error() -> None:
    resolver = make_layered_resolver({"HOME": "/home/user"})
    with pytest.raises(MissingTemplateVarError) as ei:
        substitute_string(
            "${UNDEFINED}", resolver=resolver, upstream_id="up",
        )
    assert ei.value.name == "UNDEFINED"


def test_no_transitive_user_variable_substitution() -> None:
    """User Variable values are NOT resolvers. A user var that
    references another user var stays unresolved at the layered
    resolver level — preserved by the existing
    :class:`MissingTemplateVarError` contract.
    """
    layered = make_layered_resolver(
        {"HOME": "/home/user"},
        # User var ``A`` resolves to a literal string referencing ``B``;
        # the substitution helper does NOT recurse into the resolved
        # value (it returns it verbatim).
        {"A": "${B}", "B": "real-value"},
    )
    # ``A`` resolves to the literal text ``${B}`` (one pass) — the
    # caller of substitute_string sees the unresolved ${B} only if
    # they substitute the result a SECOND time. The contract is that
    # we don't recurse: a single substitution pass returns "${B}".
    out = substitute_string("${A}", resolver=layered, upstream_id="up")
    assert out == "${B}"
