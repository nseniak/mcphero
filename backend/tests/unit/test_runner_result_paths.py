"""Each test run keeps its results to itself.

The e2e runner (``tests/run-e2e-tests.py``) and ``make test-all``
(``tests/run-all-tests.py``) used to write their logs and reports to fixed
``/tmp/mcpolis-*`` paths. On 2026-10-07, with several runs going at once,
a run printed another run's results: one run's aggregate showed another
worktree's shard 3, and a test-all run's e2e log held the other run's
output. The e2e wipe step also deleted a live run's files, and two e2e
runs started together stole each other's server ports. These tests lock
the fix: every result path a run writes, wipes or reads sits in that
run's own folder (see ``tests/run_folder.py``), the runners write the
report names test-all reads, and e2e runs take turns picking ports.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = REPO_ROOT / "tests"
BACKEND_DIR = REPO_ROOT / "backend"

# Holds the e2e port lock in a separate process, like a concurrent run.
_LOCK_HOLDER = """
import importlib.util, sys, time
from pathlib import Path
tests_dir, lock_path, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
sys.path.insert(0, tests_dir)
spec = importlib.util.spec_from_file_location("e2e", Path(tests_dir) / "run-e2e-tests.py")
module = importlib.util.module_from_spec(spec)
sys.modules["e2e"] = module
spec.loader.exec_module(module)
with module.hold_port_lock(Path(lock_path)):
    print("held", flush=True)
    time.sleep(hold)
"""


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_runner(file_name: str, name: str) -> ModuleType:
    """Import a runner script. A runner imports ``run_folder`` by that name
    (its own folder is on the path when it runs as a script), so register
    the helper under that name first."""
    _load("run_folder", TESTS_DIR / "run_folder.py")
    return _load(name, TESTS_DIR / file_name)


def _e2e_runner() -> ModuleType:
    return _load_runner("run-e2e-tests.py", "_mcpolis_run_e2e_tests")


def _test_all_runner() -> ModuleType:
    return _load_runner("run-all-tests.py", "_mcpolis_run_all_tests")


def make_e2e_run_files(e2e: ModuleType, run_dir: Path) -> set[Path]:
    """Every result file a 2-shard e2e run writes."""
    shards = [e2e.make_shard(i, 2, run_dir) for i in range(2)]
    paths = {p for s in shards for p in (s.log_path, s.json_report_path)}
    return paths | set(e2e.aggregate_paths(run_dir))


def make_budget(run_all: ModuleType, run_integration: bool = True) -> object:
    return run_all.Budget(
        cores=8, unit_jobs=2, e2e_shards=2, integration_jobs=4,
        run_integration=run_integration,
    )


def make_pytest_report(path: Path, passed: int, failed: int) -> None:
    path.write_text(json.dumps({"summary": {"passed": passed, "failed": failed}}))


def make_e2e_aggregate(path: Path, passed: int, failed: int) -> None:
    path.write_text(json.dumps(
        {"passed": passed, "failed": failed, "flaky": 0, "skipped": 0},
    ))


def make_lock_holder(lock_path: Path, hold_seconds: float) -> subprocess.Popen[str]:
    """Start a process that holds the e2e port lock; return once it does."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(TESTS_DIR), str(lock_path),
         str(hold_seconds)],
        stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def make_runner_env(out_dir: Path) -> dict[str, str]:
    """Environment for running a runner script in a folder of our own
    (outside the shared runs folder, so no shared link moves)."""
    env = dict(os.environ)
    env["MCPOLIS_TEST_OUT_DIR"] = str(out_dir)
    env["UNIT_RERUNS"] = "0"
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def test_e2e_result_files_sit_in_their_own_run_folder() -> None:
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_a, run_b = Path(tmp) / "run-a", Path(tmp) / "run-b"
        files_a = make_e2e_run_files(e2e, run_a)
        files_b = make_e2e_run_files(e2e, run_b)
        assert len(files_a) == 6
        assert all(p.parent == run_a for p in files_a)
        assert all(p.parent == run_b for p in files_b)
        assert not files_a & files_b
        assert e2e.make_shard(1, 2, run_a).artifacts_dir.parent == run_a


def test_playwright_keeps_its_failure_files_in_the_run_folder() -> None:
    """Playwright's default output folder is shared by every run in the
    checkout and emptied each time Playwright starts."""
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        shard = e2e.make_shard(0, 1, run_dir)
        argv = e2e.playwright_base_argv(shard)
        assert f"--output={run_dir / 'e2e-shard-0-artifacts'}" in argv


def test_e2e_wipe_deletes_only_its_own_run_files() -> None:
    """The wipe at the start of a run used to delete the fixed paths that
    a live concurrent run was writing."""
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_a, run_b = Path(tmp) / "run-a", Path(tmp) / "run-b"
        run_a.mkdir()
        run_b.mkdir()
        files_a = make_e2e_run_files(e2e, run_a)
        files_b = make_e2e_run_files(e2e, run_b)
        stale_extra_shard = run_a / "e2e-shard-7.log"
        for p in files_a | files_b | {stale_extra_shard}:
            p.write_text("output")
        e2e.wipe_stale_outputs(run_a)
        assert not any(p.exists() for p in files_a | {stale_extra_shard})
        assert all(p.exists() for p in files_b)


def test_e2e_aggregate_is_written_into_its_own_run_folder() -> None:
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        with redirect_stdout(io.StringIO()):
            e2e.write_aggregate(e2e.AggregateResult(passed=3), run_dir)
        assert sorted(p.name for p in run_dir.iterdir()) == [
            "e2e-aggregate.json", "e2e-aggregate.txt",
        ]
        data = json.loads((run_dir / "e2e-aggregate.json").read_text())
        assert data["passed"] == 3


def test_the_shard_log_keeps_every_writer_s_lines() -> None:
    """The servers write through the shard-log handle, while Playwright
    appends through its own. A "w" handle wrote over Playwright's lines."""
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "e2e-shard-0.log"
        path.write_text("left by an earlier run\n")
        servers = e2e.open_shard_log(path)
        servers.write("server line 1\n")
        servers.flush()
        with path.open("a") as playwright:  # as run_playwright opens it
            playwright.write("playwright line\n")
        servers.write("server line 2\n")
        servers.close()
        assert path.read_text().splitlines() == [
            "server line 1", "playwright line", "server line 2",
        ]


def test_a_second_e2e_run_waits_for_the_port_lock_and_names_the_holder() -> None:
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "ports.lock"
        holder = make_lock_holder(lock, hold_seconds=1.5)
        try:
            out = io.StringIO()
            started = time.monotonic()
            with redirect_stdout(out), e2e.hold_port_lock(lock, wait_seconds=30):
                waited = time.monotonic() - started
        finally:
            holder.wait(timeout=30)
        assert waited >= 1.0
        assert f"pid {holder.pid}" in out.getvalue()


def test_a_waiting_e2e_run_gives_up_instead_of_hanging() -> None:
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "ports.lock"
        holder = make_lock_holder(lock, hold_seconds=30)
        try:
            with redirect_stdout(io.StringIO()):
                with pytest.raises(RuntimeError, match=f"pid {holder.pid}"):
                    with e2e.hold_port_lock(lock, wait_seconds=0.5):
                        pass
        finally:
            holder.kill()
            holder.wait()


def test_a_crashed_lock_holder_frees_the_port_lock_at_once() -> None:
    e2e = _e2e_runner()
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "ports.lock"
        holder = make_lock_holder(lock, hold_seconds=30)
        holder.kill()
        holder.wait()
        started = time.monotonic()
        with redirect_stdout(io.StringIO()), e2e.hold_port_lock(lock, wait_seconds=5):
            waited = time.monotonic() - started
        assert waited < 1.0


def test_test_all_legs_log_into_the_run_folder_and_are_told_it() -> None:
    """Each leg gets the folder as MCPOLIS_TEST_OUT_DIR, so its own
    reports land next to test-all's summary."""
    run_all = _test_all_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        suites = run_all.build_suites(make_budget(run_all), run_dir)
        assert [s.name for s in suites] == ["unit", "e2e", "integration"]
        for suite in suites:
            assert suite.log_path.parent == run_dir
            assert suite.env[run_all.OUT_DIR_ENV] == str(run_dir)


def test_test_all_reports_only_its_own_run_results() -> None:
    """The 2026-10-07 symptom: a run printed another run's results."""
    run_all = _test_all_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_a, run_b = Path(tmp) / "run-a", Path(tmp) / "run-b"
        run_a.mkdir()
        run_b.mkdir()
        make_pytest_report(run_a / "unit-report.json", passed=3, failed=0)
        make_pytest_report(run_b / "unit-report.json", passed=5, failed=2)
        make_e2e_aggregate(run_a / "e2e-aggregate.json", passed=7, failed=0)
        make_e2e_aggregate(run_b / "e2e-aggregate.json", passed=9, failed=1)
        make_pytest_report(run_a / "integration-report.json", passed=4, failed=0)
        make_pytest_report(run_b / "integration-report.json", passed=6, failed=3)
        suites = run_all.build_suites(make_budget(run_all), run_a)
        for suite in suites:
            suite.returncode = 0
        results = {r.name: r for r in run_all.collect_results(suites, run_a)}
        assert (results["unit"].passed, results["unit"].failed) == (3, 0)
        assert (results["e2e"].passed, results["e2e"].failed) == (7, 0)
        assert (results["integration"].passed, results["integration"].failed) == (4, 0)
        assert all(r.ok for r in results.values())


def test_test_all_fails_a_leg_that_left_no_report() -> None:
    """A runner that renamed its report used to pass with passed=0."""
    run_all = _test_all_runner()
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        suites = run_all.build_suites(make_budget(run_all), run_dir)
        for suite in suites:
            suite.returncode = 0
        results = run_all.collect_results(suites, run_dir)
        assert [r.ok for r in results] == [False, False, False]
        assert all("no/invalid" in r.note for r in results)


def test_the_unit_runner_writes_the_report_test_all_reads() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        result = subprocess.run(
            ["bash", str(BACKEND_DIR / "run-unit-tests.sh"), "-j", "1", "-q",
             "tests/unit/test_run_folder.py::test_the_command_line_needs_exactly_one_suite"],
            env=make_runner_env(out), capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((out / "unit-report.json").read_text())
        assert report["summary"]["passed"] == 1
        assert (out / "unit-junit.xml").is_file()
        last_line = result.stdout.strip().splitlines()[-1]
        assert last_line == f"Results folder: {out.resolve()}"


def test_the_integration_runner_writes_the_report_test_all_reads() -> None:
    """Collection only, so no paid test runs."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        result = subprocess.run(
            ["bash", str(BACKEND_DIR / "run-integration-tests.sh"), "-j", "1",
             "--collect-only", "-q",
             "tests/integration/test_e2b_sandbox_service_real_sdk.py"],
            env=make_runner_env(out), capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "summary" in json.loads((out / "integration-report.json").read_text())
        assert (out / "integration-junit.xml").is_file()
