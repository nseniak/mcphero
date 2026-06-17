#!/usr/bin/env python3
"""Run the unit + e2e + integration suites concurrently, green under load.

``make test-all`` -> this. The three suites each pass on their own, but run
together at full tilt they oversubscribe the box and produce load-induced
flakes that vanish when each runs alone: the in-process MCP unit tests time
out their loopback connect, and the Vite dev server drops sockets under a
starved CPU. The fix is NOT to serialize them — it's to run all three in
parallel but bound TOTAL concurrency to the host's core count, so the box
stays busy without starving. Each suite still owns its own isolation
(unit: throwaway Mongo DBs on :27017; e2e: the ``test`` compose profile on
:27018 / :6380 plus a per-shard stack on the 1xxxx ports; integration: hosted
E2B, no local infra), so the only shared, finite resource is CPU — and that's
what this script rations.

Core budget (host has N cores; override any piece via env):

    e2e_shards      = E2E_SHARDS       or clamp(N // 3, 2, 4)
    unit_jobs       = UNIT_JOBS        or clamp(N - e2e_shards*2 - 2, 2, N)
    integration_jobs= INTEGRATION_JOBS or 4   (network-bound; ~0 local CPU)

Each e2e shard runs ~2 CPU-hungry processes (a Vite dev server + a Chromium),
so it's weighted at 2; integration is E2B-bound and weighted at ~0. The
default split leaves ~2 cores of headroom. On a 14-core box that's 4 e2e
shards + unit ``-j 4`` + integration ``-j 4``.

Why bound *unit* rather than shave e2e shards: the e2e suite's bin-packer
partition is tuned at 4 shards, and a couple of stateful serial specs
(e.g. 34-plan-gates) assume a lightly-loaded shard. Dropping to 3 shards
repacks 19+ specs onto one shard and starves those specs' UI polls. Capping
unit's worker count frees the same CPU without disturbing the e2e partition,
so e2e behaves exactly as a standalone ``--shards 4`` run.

Knobs:
    NO_INTEGRATION=1        skip the (paid) E2B integration leg entirely
    UNIT_JOBS / E2E_SHARDS / INTEGRATION_JOBS   override the budget pieces
    E2E_RETRIES / E2E_TIMEOUT_MS                forwarded to Playwright
                                               (default 2 / 45000 here)
    TEST_ALL_CORES          pretend the host has this many cores (testing)

Outputs:
    /tmp/mcpolis-all-unit.log / -e2e.log / -integration.log   per-suite logs
    plus each suite's own JSON report (read back for the aggregate):
      unit         /tmp/mcpolis-unit-report.json
      e2e          /tmp/mcpolis-e2e-aggregate.json
      integration  /tmp/mcpolis-integration-report.json
    /tmp/mcpolis-all-aggregate.txt   the combined summary printed at the end

Exit code is non-zero if ANY suite's process exits non-zero or reports a
failure.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

UNIT_REPORT = Path("/tmp/mcpolis-unit-report.json")
E2E_AGGREGATE = Path("/tmp/mcpolis-e2e-aggregate.json")
INTEGRATION_REPORT = Path("/tmp/mcpolis-integration-report.json")


def _env_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[test-all] ignoring non-integer {name}={raw!r}", file=sys.stderr)
        return default


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


@dataclass
class Budget:
    cores: int
    unit_jobs: int
    e2e_shards: int
    integration_jobs: int
    run_integration: bool


def compute_budget() -> Budget:
    cores = _env_int("TEST_ALL_CORES", None) or os.cpu_count() or 8
    no_integration = os.environ.get("NO_INTEGRATION", "") not in ("", "0", "false")
    run_integration = not no_integration

    e2e_shards = _env_int("E2E_SHARDS", None)
    if e2e_shards is None:
        # Bias toward 4 (the e2e suite's tuned partition) on a roomy box,
        # scale down on small ones.
        e2e_shards = _clamp(cores // 3, 2, 4)

    unit_jobs = _env_int("UNIT_JOBS", None)
    if unit_jobs is None:
        # e2e shards weigh ~2 CPUs each; leave ~2 cores of headroom for the
        # OS and integration's light load.
        unit_jobs = _clamp(cores - e2e_shards * 2 - 2, 2, cores)

    integration_jobs = _env_int("INTEGRATION_JOBS", None) or 4

    return Budget(
        cores=cores,
        unit_jobs=unit_jobs,
        e2e_shards=e2e_shards,
        integration_jobs=integration_jobs,
        run_integration=run_integration,
    )


@dataclass
class Suite:
    name: str
    argv: list[str]
    log_path: Path
    env: dict[str, str]
    proc: subprocess.Popen[bytes] | None = None
    log_handle: Any = field(default=None, repr=False)
    returncode: int | None = None
    started_at: float = 0.0
    finished_at: float = 0.0


def build_suites(budget: Budget) -> list[Suite]:
    base_env = os.environ.copy()
    suites: list[Suite] = [
        Suite(
            name="unit",
            argv=[
                "bash", str(BACKEND_DIR / "run-unit-tests.sh"),
                "-j", str(budget.unit_jobs),
            ],
            log_path=Path("/tmp/mcpolis-all-unit.log"),
            env=base_env,
        ),
        Suite(
            name="e2e",
            argv=[
                "bash", str(REPO_ROOT / "tests" / "run-e2e-tests.sh"),
                "--shards", str(budget.e2e_shards),
            ],
            log_path=Path("/tmp/mcpolis-all-e2e.log"),
            env={
                **base_env,
                # Loosen Playwright under cross-suite load: a double blip
                # (both attempts starved at once) still recovers, and a
                # genuinely slow nav doesn't trip the 30s default.
                "E2E_RETRIES": os.environ.get("E2E_RETRIES", "2"),
                "E2E_TIMEOUT_MS": os.environ.get("E2E_TIMEOUT_MS", "45000"),
            },
        ),
    ]
    if budget.run_integration:
        suites.append(
            Suite(
                name="integration",
                argv=[
                    "bash", str(BACKEND_DIR / "run-integration-tests.sh"),
                    "-j", str(budget.integration_jobs),
                ],
                log_path=Path("/tmp/mcpolis-all-integration.log"),
                env=base_env,
            )
        )
    return suites


def launch(suite: Suite) -> None:
    suite.log_handle = suite.log_path.open("w")
    suite.started_at = time.time()
    # ``start_new_session`` makes each suite a process-group leader so we can
    # signal the whole tree on teardown (the bash wrappers fork pytest /
    # node / docker children).
    suite.proc = subprocess.Popen(
        suite.argv,
        cwd=str(REPO_ROOT),
        env=suite.env,
        stdout=suite.log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def terminate_all(suites: list[Suite]) -> None:
    for s in suites:
        if s.proc is not None and s.proc.poll() is None:
            try:
                os.killpg(os.getpgid(s.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    deadline = time.time() + 10
    for s in suites:
        if s.proc is None:
            continue
        try:
            s.proc.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(s.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


# ─── Per-suite result parsing ─────────────────────────────────────────────


@dataclass
class SuiteResult:
    name: str
    returncode: int
    passed: int = 0
    failed: int = 0
    flaky: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.failed == 0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_pytest_report(name: str, suite: Suite, path: Path) -> SuiteResult:
    rc = suite.returncode if suite.returncode is not None else -1
    res = SuiteResult(name=name, returncode=rc,
                      duration_s=suite.finished_at - suite.started_at)
    data = _read_json(path)
    if data is None:
        res.note = f"no/invalid JSON report at {path}"
        return res
    summary = data.get("summary", {})
    res.passed = int(summary.get("passed", 0))
    res.failed = int(summary.get("failed", 0)) + int(summary.get("error", 0))
    res.skipped = int(summary.get("skipped", 0))
    return res


def parse_e2e_aggregate(suite: Suite) -> SuiteResult:
    rc = suite.returncode if suite.returncode is not None else -1
    res = SuiteResult(name="e2e", returncode=rc,
                      duration_s=suite.finished_at - suite.started_at)
    data = _read_json(E2E_AGGREGATE)
    if data is None:
        res.note = f"no/invalid aggregate at {E2E_AGGREGATE}"
        return res
    res.passed = int(data.get("passed", 0))
    res.failed = int(data.get("failed", 0))
    res.flaky = int(data.get("flaky", 0))
    res.skipped = int(data.get("skipped", 0))
    return res


def collect_results(suites: list[Suite]) -> list[SuiteResult]:
    out: list[SuiteResult] = []
    for s in suites:
        if s.name == "unit":
            out.append(parse_pytest_report("unit", s, UNIT_REPORT))
        elif s.name == "integration":
            out.append(parse_pytest_report("integration", s, INTEGRATION_REPORT))
        elif s.name == "e2e":
            out.append(parse_e2e_aggregate(s))
    return out


# ─── Reporting ────────────────────────────────────────────────────────────


def render_summary(results: list[SuiteResult], budget: Budget,
                   wall_s: float) -> str:
    lines = [
        "═" * 64,
        f"test-all: {budget.cores} cores -> unit -j{budget.unit_jobs}, "
        f"e2e --shards {budget.e2e_shards}, "
        + (f"integration -j{budget.integration_jobs}"
           if budget.run_integration else "integration SKIPPED"),
        "─" * 64,
    ]
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        extra = f" flaky={r.flaky}" if r.flaky else ""
        note = f"  ({r.note})" if r.note else ""
        lines.append(
            f"  {status}  {r.name:<12} passed={r.passed} failed={r.failed}"
            f"{extra} skipped={r.skipped} rc={r.returncode} "
            f"({r.duration_s:.0f}s){note}"
        )
    overall = all(r.ok for r in results)
    lines.append("─" * 64)
    lines.append(f"  OVERALL: {'PASS' if overall else 'FAIL'} "
                 f"(wall {wall_s:.0f}s)")
    lines.append("═" * 64)
    return "\n".join(lines) + "\n"


def main() -> int:
    budget = compute_budget()
    print(
        f"[test-all] {budget.cores} cores -> unit -j{budget.unit_jobs}, "
        f"e2e --shards {budget.e2e_shards}, "
        + (f"integration -j{budget.integration_jobs}"
           if budget.run_integration else "integration SKIPPED"),
        flush=True,
    )

    suites = build_suites(budget)
    started = time.time()

    def _handle_signal(sig: int, _frame: Any) -> None:
        print(f"\n[test-all] signal {sig} — tearing down suites...", flush=True)
        terminate_all(suites)
        sys.exit(130)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        for s in suites:
            launch(s)
            print(f"[test-all] launched {s.name}: {' '.join(s.argv)} "
                  f"-> {s.log_path}", flush=True)

        # Poll until all suites exit, emitting a compact heartbeat.
        last_beat = 0.0
        while any(s.proc is not None and s.proc.poll() is None for s in suites):
            now = time.time()
            for s in suites:
                if s.proc is not None and s.returncode is None \
                        and s.proc.poll() is not None:
                    s.returncode = s.proc.returncode
                    s.finished_at = now
                    if s.log_handle:
                        s.log_handle.flush()
                    print(f"[test-all] {s.name} finished rc={s.returncode} "
                          f"({s.finished_at - s.started_at:.0f}s)", flush=True)
            if now - last_beat >= 15:
                running = [s.name for s in suites
                           if s.proc is not None and s.proc.poll() is None]
                if running:
                    print(f"[test-all] {now - started:.0f}s elapsed; "
                          f"running: {', '.join(running)}", flush=True)
                last_beat = now
            time.sleep(2)

        # Capture any still-unrecorded return codes / finish times.
        now = time.time()
        for s in suites:
            if s.proc is not None and s.returncode is None:
                s.returncode = s.proc.returncode
                s.finished_at = now
            if s.log_handle:
                s.log_handle.close()
    finally:
        terminate_all(suites)

    wall = time.time() - started
    results = collect_results(suites)
    summary = render_summary(results, budget, wall)
    Path("/tmp/mcpolis-all-aggregate.txt").write_text(summary)
    print(summary, end="")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
