"""Guard for the operator's orphan-cleanup script.

Without the persisted-id list nothing is "recognized", so
``--delete-orphans`` used to kill every sandbox older than the age gate
in the whole E2B account, live mcpolis ones included. It must refuse
before touching E2B.
"""
from __future__ import annotations

import pytest

from tests.integration import list_orphan_sandboxes as script


def test_delete_refusal_without_persisted_ids() -> None:
    assert script._delete_refusal(set()) is not None  # pyright: ignore[reportPrivateUsage]


def test_delete_allowed_with_persisted_ids() -> None:
    assert script._delete_refusal({"sbx-live"}) is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_main_refuses_delete_orphans_without_persisted_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Exits 2 with the refusal, before the E2B client is built."""
    monkeypatch.delenv("MCPOLIS_PERSISTED_SANDBOX_IDS", raising=False)

    code = await script.main(["--delete-orphans"])

    assert code == 2
    assert "Refusing" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_refuses_delete_orphans_with_run_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """``--run-id`` is a read-only view; it never widens a delete."""
    monkeypatch.setenv("MCPOLIS_PERSISTED_SANDBOX_IDS", "sbx-live")

    code = await script.main(["--delete-orphans", "--run-id", "a1b2c3d4e5f6"])

    assert code == 2
    assert "read-only" in capsys.readouterr().err
