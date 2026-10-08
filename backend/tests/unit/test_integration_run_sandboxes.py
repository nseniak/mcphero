"""Guard for the paid integration tests' end-of-run sandbox cleanup.

The tests share their E2B account with production. At the end of a run
they kill the sandboxes that run created: the selection must pick every
sandbox carrying this run's id, and never one with another instance tag,
another run's id, or a production tag.
"""
from __future__ import annotations

import pytest

from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from tests.integration._run_sandboxes import (
    is_run_sandbox,
    kill_run_sandboxes,
    new_run_id,
    select_run_sandboxes,
)
from tests.unit.sandbox_e2b_mock import (
    MockE2BClient,
    MockE2BSandboxInfo,
    make_mock_e2b_client,
)

RUN_ID = "a1b2c3d4e5f6"
OTHER_RUN_ID = "0f9e8d7c6b5a"


def make_info(sandbox_id: str, metadata: dict[str, str]) -> MockE2BSandboxInfo:
    return MockE2BSandboxInfo(
        sandbox_id=sandbox_id, state="paused", metadata=metadata,
    )


def make_service_metadata(instance: str, org: str) -> dict[str, str]:
    """The tags ``E2BSandboxService`` puts on a sandbox it creates."""
    return {
        "mcpolis_instance": instance,
        "mcpolis_org": org,
        "mcpolis_upstream": "e2e-upstream",
    }


def make_direct_metadata(run_id: str) -> dict[str, str]:
    """The tags a test puts on a sandbox it creates through the client."""
    return {"mcpolis_test": "1", "test_run_id": run_id, "scenario": "x"}


def make_account() -> MockE2BClient:
    """A shared E2B account: this run's sandboxes among others'."""
    client = make_mock_e2b_client()
    client.live_infos = [
        make_info("mine-service", make_service_metadata(
            f"e2e-m6-{RUN_ID}", f"acme-m6-{RUN_ID}",
        )),
        make_info("mine-suffixed", make_service_metadata(
            f"e2e-{RUN_ID}-fresh-a", f"acme-{RUN_ID}",
        )),
        make_info("mine-direct", make_direct_metadata(RUN_ID)),
        make_info("other-run", make_service_metadata(
            f"e2e-m6-{OTHER_RUN_ID}", f"acme-m6-{OTHER_RUN_ID}",
        )),
        make_info("other-run-direct", make_direct_metadata(OTHER_RUN_ID)),
        make_info("prod", make_service_metadata(
            "6f1c2d3e-4b5a-4c6d-8e7f-a1b2c3d4e5f6",
            "0b8e3c1a-2d4f-4e6a-9b7c-5d3e1f0a2b4c",
        )),
    ]
    return client


def test_selects_service_sandboxes_of_this_run() -> None:
    assert is_run_sandbox(make_service_metadata(f"e2e-m6-{RUN_ID}", "o"), RUN_ID)
    assert is_run_sandbox(
        make_service_metadata(f"e2e-{RUN_ID}-fresh-a", "o"), RUN_ID,
    )


def test_selects_direct_sandboxes_of_this_run() -> None:
    assert is_run_sandbox(make_direct_metadata(RUN_ID), RUN_ID)


def test_never_selects_another_run() -> None:
    assert not is_run_sandbox(
        make_service_metadata(f"e2e-m6-{OTHER_RUN_ID}", f"acme-{RUN_ID}"), RUN_ID,
    )
    assert not is_run_sandbox(make_direct_metadata(OTHER_RUN_ID), RUN_ID)


def test_never_selects_a_production_instance_holding_the_id() -> None:
    """A production instance id is not ``e2e-``-prefixed, even when one
    of its parts happens to equal the run id."""
    prod_instance = f"6f1c2d3e-4b5a-4c6d-8e7f-{RUN_ID}"
    assert not is_run_sandbox(make_service_metadata(prod_instance, "o"), RUN_ID)


def test_never_selects_by_org_alone() -> None:
    assert not is_run_sandbox({"mcpolis_org": f"acme-{RUN_ID}"}, RUN_ID)


def test_never_selects_a_partial_id_match() -> None:
    assert not is_run_sandbox(
        make_service_metadata(f"e2e-m6-{RUN_ID}x", "o"), RUN_ID,
    )
    assert not is_run_sandbox(
        make_service_metadata(f"e2e-m6-{RUN_ID}", "o"), RUN_ID[:8],
    )


def test_direct_tag_needs_the_test_marker() -> None:
    assert not is_run_sandbox({"test_run_id": RUN_ID}, RUN_ID)


def test_a_run_id_not_shaped_like_one_selects_nothing() -> None:
    """Short or non-hex ids would match the parts every test instance
    shares (``e2e``, ``m6``) and select other runs' sandboxes."""
    client = make_account()
    for bad in ("", "e2e", "m6", "acme", f"m6-{RUN_ID}", RUN_ID.upper()):
        assert select_run_sandboxes(client.live_infos, bad) == [], bad


def test_new_run_ids_differ_and_select_their_own_sandboxes() -> None:
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert is_run_sandbox(make_direct_metadata(first), first)
    assert is_run_sandbox(make_service_metadata(f"e2e-x-{first}", "o"), first)


def test_select_picks_exactly_this_runs_sandboxes() -> None:
    client = make_account()

    picked = select_run_sandboxes(client.live_infos, RUN_ID)

    assert sorted(s.sandbox_id for s in picked) == [
        "mine-direct", "mine-service", "mine-suffixed",
    ]


@pytest.mark.asyncio
async def test_kill_removes_only_this_runs_sandboxes() -> None:
    client = make_account()

    report = await kill_run_sandboxes(client, RUN_ID)

    assert sorted(report.killed) == ["mine-direct", "mine-service", "mine-suffixed"]
    assert report.failed == []
    assert sorted(i.sandbox_id for i in client.live_infos) == [
        "other-run", "other-run-direct", "prod",
    ]


@pytest.mark.asyncio
async def test_failed_kill_is_reported_not_raised() -> None:
    client = make_account()
    client.kill_raises = E2BSDKError("RateLimitException", "slow down")

    report = await kill_run_sandboxes(client, RUN_ID)

    assert report.killed == []
    assert len(report.failed) == 3
    assert any("FAILED" in line for line in report.lines())
