"""Read-only built-in **system Variables** injected into the
``${...}`` namespace at sandbox-launch time.

v1 ships exactly one entry: ``HOME``. Operators reference it the
same way they reference any user Variable —
``${HOME}/.config/gcloud/credentials.json`` — and the substitution
helper resolves the token uniformly. No special-case ``~/`` parsing,
no per-source prefix.

System variable names live in the **same** ``${...}`` namespace as
user Variables and Sandbox Files; the write-side rejects any user
Variable or Sandbox File that shares a name with a system entry.
See plan §C "Shared namespace, enforced uniqueness".

The 16 E2B templates in ``runner/e2b-templates/`` all FROM
``node:22-bookworm-slim`` or ``python:3.12-slim-bookworm`` — neither
Dockerfile sets ``WORKDIR`` / ``USER``, so E2B's default
``user``-with-``/home/user`` applies uniformly. Verified
2026-05-05; if a future template diverges, fetch ``$HOME`` from the
live sandbox at launch (the launcher already runs a setup step
where this is cheap) instead of bumping this constant.
"""
from __future__ import annotations


# E2B's default sandbox user is ``user`` with ``HOME=/home/user``.
# Hardcoded because every published mcpolis template inherits the
# stock SDK user; the consistency guard test at
# ``tests/unit/test_e2b_template_home_consistency.py`` flags any
# template Dockerfile that overrides USER / WORKDIR / HOME so this
# constant can never silently lie.
DEFAULT_SANDBOX_HOME: str = "/home/user"


def system_variables_for_sandbox() -> dict[str, str]:
    """Build the system-Variable map injected at substitution time.

    Returns a fresh dict (callers free to mutate). v1 ships a single
    entry; future additions (`USER`, `SANDBOX_ID`, …) plug in here
    without touching the substitution layer.
    """
    return {"HOME": DEFAULT_SANDBOX_HOME}


def system_variable_names() -> set[str]:
    """Reserved names that user Variables / Sandbox Files can't shadow."""
    return set(system_variables_for_sandbox().keys())


def is_system_variable_name(name: str) -> bool:
    return name in system_variable_names()
