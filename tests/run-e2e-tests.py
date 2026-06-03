#!/usr/bin/env python3
"""Playwright E2E test orchestrator with optional sharding.

Replaces the historic ``run-e2e-tests.sh``. With ``--shards 1`` (the
default) behaves like the bash version: one backend stack, one
Playwright run, single combined log. With ``--shards N`` brings up N
fully-independent stacks on disjoint ports + databases and runs N
Playwright processes in parallel, each owning ``--shard k/N`` of the
specs.

Why this lives in Python and not bash:
    The single-shard bash version was ~200 lines. Sharding adds port
    arithmetic, parallel process tree management, per-shard log
    multiplexing, JSON aggregation, and parallel teardown. Each piece
    is bash-doable individually; together they cross the threshold
    where shell stops being the right tool.

Why isolated mongo / redis containers (test profile):
    The ``test`` compose profile (mongo-test on 27018, redis-test on
    6380) is intentionally separate from the ``dev`` profile a
    developer's ``start.sh`` brings up. A dev can keep working on the
    canonical 27017 / 6379 / 8080 / 5173 stack while tests run — no
    port collision, no risk of a destructive test dropping a dev DB.

Outputs (per shard ``k`` ∈ [0, N)):
    /tmp/mcpolis-e2e-shard-{k}.log     combined log (backend + vite +
                                       MCP fakes + playwright stdout)
    /tmp/mcpolis-e2e-shard-{k}.json    Playwright JSON reporter output
    /tmp/mcpolis-e2e-aggregate.json    aggregated pass/fail summary
    /tmp/mcpolis-e2e-aggregate.txt     human-readable summary
"""

from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
E2E_DIR = REPO_ROOT / "tests" / "e2e"

# Per-spec wall-clock cache. Refreshed after every run with measured
# durations from each shard's Playwright JSON report. Used by the
# bin-packer to balance work across shards. Missing entries fall back
# to the median duration so a brand-new spec gets a reasonable initial
# placement.
SPEC_TIMES_CACHE = Path("/tmp/mcpolis-e2e-spec-times.json")

# Test infra ports (compose profile=test). These are intentionally
# different from the dev profile (27017 / 6379) so a developer can
# keep working on their canonical stack while tests run.
TEST_MONGO_PORT = 27018
TEST_REDIS_PORT = 6380
TEST_MONGO_CONTAINER = "mcpolis-mongo-test"
TEST_REDIS_CONTAINER = "mcpolis-redis-test"

# Per-shard port plan. Tests live in the 1xxxx range so they never
# collide with the dev stack on 8080 / 5173. See CLAUDE.md
# "test port range" for the convention.
#
# These are *preferred* bases, not hard assignments: ``make_shard``
# probes upward from each base for the first free port (see
# ``_find_free_port``). When a base is free a run lands on exactly
# these numbers (predictable for a lone run / muscle memory); when a
# base is occupied — a leaked orphan we couldn't reap, or a second
# ``--shards`` run sharing the host — the shard spills to the next
# free port instead of silently binding atop a squatter. This is what
# makes overlapping runs fail-soft instead of cascading.
SHARD_BACKEND_BASE = 18080  # +N*10
SHARD_FRONTEND_BASE = 15173  # +N*10
SHARD_DEMO_MCP_BASE = 19999  # +N*10
SHARD_OAUTH_MCP_BASE = 19998  # +N*10

# A short per-process token mixed into the per-shard Mongo DB name so
# two concurrent orchestrator runs (e.g. an operator's run + a CI run
# on the same Mongo daemon) don't both target ``mcpolis_e2e_s0`` and
# trample each other's data. ``secrets.token_hex`` is fine here — this
# script is not resumable, so we don't need determinism across runs.
RUN_TOKEN = secrets.token_hex(3)

# Commandline fragments that identify a process as one of *our* e2e
# children. Used by the pre-flight reaper to distinguish a leaked
# orphan (safe to kill) from an unrelated process that merely happens
# to hold a port in our range. Deliberately specific: the dev stack
# (``python -m mcpolis`` on 8080, ``vite`` with no ``--port 15``) does
# NOT match, so the reaper can never touch it.
_E2E_PROCESS_MARKERS = (
    "tests/e2e/test_mcp_server.py",
    "tests/e2e/oauth_test_mcp_server.py",
    "vite --port 15",
    "run-e2e-tests.py",
)


def _port_is_free(port: int) -> bool:
    """True if a fresh listening socket can bind ``port`` on loopback.

    No ``SO_REUSEADDR`` — we *want* the bind to fail when something is
    already listening, so a squatter is detected rather than silently
    shared."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_free_port(preferred: int, span: int = 400) -> int:
    """Return ``preferred`` if free, else the next free port above it.

    Scans ``[preferred, preferred + span)`` so a spilled shard stays
    within a recognisable band (e.g. backend ports remain ~18xxx).
    Falls back to an OS-assigned ephemeral port if the whole band is
    occupied (pathological — hundreds of stale listeners)."""
    for candidate in range(preferred, preferred + span):
        if _port_is_free(candidate):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ─── Shard configuration ──────────────────────────────────────────────


@dataclass
class ShardConfig:
    index: int
    total: int
    backend_port: int
    frontend_port: int
    demo_mcp_port: int
    oauth_mcp_port: int
    db_name: str
    log_path: Path
    json_report_path: Path

    @property
    def backend_url(self) -> str:
        return f"http://localhost:{self.backend_port}"

    @property
    def frontend_url(self) -> str:
        return f"http://localhost:{self.frontend_port}"

    @property
    def demo_mcp_url(self) -> str:
        return f"http://localhost:{self.demo_mcp_port}"

    @property
    def oauth_mcp_url(self) -> str:
        return f"http://localhost:{self.oauth_mcp_port}"


def make_shard(index: int, total: int) -> ShardConfig:
    # Probe upward from each preferred base for a free port. The four
    # bases are >=10 apart, so a lone run lands on the historic
    # 18080/15173/19999/19998+index*10 numbers; a run sharing the host
    # with a leftover/concurrent run spills to the next free port
    # rather than colliding. Log paths stay index-keyed (stable for
    # grep) regardless of which ports get chosen.
    return ShardConfig(
        index=index,
        total=total,
        backend_port=_find_free_port(SHARD_BACKEND_BASE + index * 10),
        frontend_port=_find_free_port(SHARD_FRONTEND_BASE + index * 10),
        demo_mcp_port=_find_free_port(SHARD_DEMO_MCP_BASE + index * 10),
        oauth_mcp_port=_find_free_port(SHARD_OAUTH_MCP_BASE + index * 10),
        db_name=f"mcpolis_e2e_{RUN_TOKEN}_s{index}",
        log_path=Path(f"/tmp/mcpolis-e2e-shard-{index}.log"),
        json_report_path=Path(f"/tmp/mcpolis-e2e-shard-{index}.json"),
    )


# ─── Process registry (for clean teardown) ────────────────────────────


_processes: list[subprocess.Popen[bytes]] = []
_processes_lock = Lock()


def _register(proc: subprocess.Popen[bytes]) -> subprocess.Popen[bytes]:
    with _processes_lock:
        _processes.append(proc)
    return proc


def _signal_proc_group(p: subprocess.Popen[bytes], sig: int) -> None:
    """Send ``sig`` to a child's whole process group, falling back to
    the lone PID.

    Children are spawned with ``start_new_session=True``, so each is a
    process-group leader (pgid == pid). Signalling the *group* — not
    just ``p.pid`` — reaches grandchildren too: ``npm run dev`` forks
    ``node``/``vite`` (the actual holder of the 15xxx port), and a bare
    ``p.terminate()`` to npm doesn't reliably reach vite, orphaning it
    to squat the port for the next run. ``killpg`` closes that leak."""
    try:
        os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            p.send_signal(sig)


def _terminate_all() -> None:
    with _processes_lock:
        procs = list(_processes)
        _processes.clear()
    for p in procs:
        _signal_proc_group(p, signal.SIGTERM)
    deadline = time.time() + 5.0
    for p in procs:
        remaining = max(0.1, deadline - time.time())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            # Hard-kill the whole group — a graceful SIGTERM didn't
            # land in time (e.g. a backend mid-drain, or a wedged vite).
            _signal_proc_group(p, signal.SIGKILL)


# ─── Test infra (mongo-test + redis-test) ─────────────────────────────


def _container_running(name: str) -> bool:
    try:
        out = subprocess.check_output(
            [
                "docker", "ps", "--filter", f"name=^{name}$",
                "--filter", "status=running", "--format", "{{.Names}}",
            ],
            text=True,
        )
        return name in out.strip().splitlines()
    except subprocess.CalledProcessError:
        return False


def ensure_test_infra() -> None:
    """Bring up the ``test`` compose profile (mongo-test + redis-test)
    if not already running. Idempotent."""
    mongo_up = _container_running(TEST_MONGO_CONTAINER)
    redis_up = _container_running(TEST_REDIS_CONTAINER)
    if mongo_up and redis_up:
        print(f"[infra] test containers already up "
              f"(mongo:{TEST_MONGO_PORT} redis:{TEST_REDIS_PORT})")
        return
    print("[infra] starting test containers (mongo-test + redis-test)...")
    subprocess.check_call(
        ["docker", "compose", "--profile", "test", "up", "-d"],
        cwd=REPO_ROOT,
    )
    # Wait for mongo to accept connections.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            subprocess.check_output(
                [
                    "docker", "exec", TEST_MONGO_CONTAINER,
                    "mongosh", "--quiet", "--eval", "db.runCommand({ping:1})",
                ],
                stderr=subprocess.DEVNULL,
            )
            print("[infra] test mongo ready")
            break
        except subprocess.CalledProcessError:
            time.sleep(0.5)
    else:
        raise RuntimeError("test mongo did not become ready within 30s")


def teardown_test_infra() -> None:
    print("[infra] stopping test containers...")
    subprocess.run(
        ["docker", "compose", "--profile", "test", "down"],
        cwd=REPO_ROOT,
        check=False,
    )


def drop_test_db(db_name: str) -> None:
    subprocess.run(
        [
            "docker", "exec", TEST_MONGO_CONTAINER, "mongosh", "--quiet",
            "--eval", f"db.getSiblingDB('{db_name}').dropDatabase()",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ─── Pre-flight orphan reaper ─────────────────────────────────────────


def _in_band_ports(total_shards: int) -> set[int]:
    """All preferred ports a run with ``total_shards`` shards could land
    on, including the upward spill span. A listener on one of these is a
    candidate squatter."""
    span = 400  # matches ``_find_free_port`` default
    ports: set[int] = set()
    for base in (
        SHARD_BACKEND_BASE, SHARD_FRONTEND_BASE,
        SHARD_DEMO_MCP_BASE, SHARD_OAUTH_MCP_BASE,
    ):
        ports.update(range(base, base + total_shards * 10 + span))
    return ports


def _listeners_in_band(total_shards: int) -> list[tuple[int, int]]:
    """Return ``(pid, port)`` for every TCP listener bound to an in-band
    port. Best-effort: if ``lsof`` is missing/unhappy, returns []."""
    band = _in_band_ports(total_shards)
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    found: list[tuple[int, int]] = []
    for line in out.splitlines()[1:]:  # skip the header row
        fields = line.split()
        if len(fields) < 9:
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        # The NAME column is the address (``127.0.0.1:18100`` /
        # ``[::1]:15173``), but lsof appends a parenthesised state —
        # ``(LISTEN)`` — as a trailing token even under ``-sTCP:LISTEN``.
        # Drop it before reading the port, or every row parses to a
        # non-integer and the reaper silently no-ops.
        if fields[-1].startswith("(") and fields[-1].endswith(")"):
            name = fields[-2]
        else:
            name = fields[-1]
        port_str = name.rsplit(":", 1)[-1]
        try:
            port = int(port_str)
        except ValueError:
            continue
        if port in band:
            found.append((pid, port))
    return found


def _proc_ppid_and_cmd(pid: int) -> tuple[int, str]:
    """``(ppid, full-commandline)`` for ``pid`` via ``ps``; ``(-1, "")``
    if the process is gone."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return -1, ""
    if not out:
        return -1, ""
    ppid_str, _, cmd = out.partition(" ")
    try:
        return int(ppid_str), cmd.strip()
    except ValueError:
        return -1, cmd.strip()


def reap_leaked_e2e_orphans(total_shards: int) -> None:
    """Kill leftover e2e child processes squatting an in-band port.

    Triple-guarded so it can NEVER touch the dev stack or a *live*
    concurrent run:

    1. The process must be listening on a port inside our preferred
       bands (``_in_band_ports``). The dev stack (8080 / 5173) is out
       of band.
    2. Its full commandline must match one of ``_E2E_PROCESS_MARKERS``
       (our test MCP fakes, a ``vite --port 15…``, or ``-m mcpolis``).
       The dev Vite (no ``--port 15``) does not match.
    3. Its parent must be ``init``/launchd (ppid == 1) — i.e. it was
       *orphaned* when its orchestrator died. A live concurrent run's
       children are parented to that run, not to pid 1, so they are
       left alone; dynamic ports keep us from colliding with them.

    Only the intersection of all three is reaped — a genuine leak from
    an interrupted prior run."""
    reaped = 0
    for pid, port in _listeners_in_band(total_shards):
        ppid, cmd = _proc_ppid_and_cmd(pid)
        if ppid != 1:
            continue
        if not any(marker in cmd for marker in _E2E_PROCESS_MARKERS):
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(pid, signal.SIGKILL)
        reaped += 1
        print(
            f"[reaper] killed leaked orphan pid={pid} port={port} "
            f"cmd={cmd[:80]!r}",
        )
    if reaped:
        # Give the OS a moment to release the freed ports before
        # ``make_shard`` probes them.
        time.sleep(0.5)
        print(f"[reaper] reaped {reaped} leaked e2e orphan(s)")


# ─── Polling helpers ──────────────────────────────────────────────────


def wait_for_url(
    url: str, timeout: float, label: str,
    proc: subprocess.Popen[bytes] | None = None,
) -> None:
    """Poll ``url`` until it responds with anything HTTP-shaped.

    A 4xx counts as ``up`` — the demo MCP server returns 406 on a bare
    GET (it wants the streamable-HTTP Accept headers), and the OAuth
    fake's ``.well-known/oauth-protected-resource`` returns 200, but
    both prove the process is live and listening. Only network-level
    errors (refused, unreachable, timeout) mean ``not yet ready``.

    ``proc`` (when given) is the child we expect to be answering. If it
    *exits* while we're polling, we fail immediately with a clear error
    instead of waiting out the timeout — or worse, returning happily
    because some *other* process (a leaked orphan, a concurrent run) is
    squatting the port and answering for it. This is the guard that
    turns the old "shard marked ready atop a squatter → every spec
    503s" cascade into a single, honest bootstrap failure.
    """
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"{label} process exited (rc={proc.returncode}) before "
                f"{url} came up — its port was almost certainly already "
                f"in use. Check the shard log for a bind error.",
            )
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except urllib.error.HTTPError:
            # Got a real HTTP response — process is up.
            return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(0.3)
    raise RuntimeError(f"timed out waiting for {label} ({url}): {last_err}")


def wait_for_port_free(port: int, timeout: float = 10) -> None:
    """Best-effort wait for a port to be free (after a Popen termination
    the OS may take a moment to release the bind). Doesn't fail if the
    port is still bound — caller will get a clear bind error if so."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket()
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            sock.close()
            time.sleep(0.2)
        except (ConnectionRefusedError, socket.timeout, OSError):
            return


# ─── Shard bootstrap ──────────────────────────────────────────────────


@dataclass
class ShardProcesses:
    backend: subprocess.Popen[bytes]
    frontend: subprocess.Popen[bytes]
    demo_mcp: subprocess.Popen[bytes]
    oauth_mcp: subprocess.Popen[bytes]
    log_handle: Any = field(repr=False, default=None)


def _build_backend_env(shard: ShardConfig) -> dict[str, str]:
    """Build the env dict used for the backend uvicorn process.

    Mirrors what ``start.sh cloud --fake-auth`` would set, but pinned
    to test infra and the shard's ports."""
    env = os.environ.copy()
    # Strip any inherited MCPOLIS_* that would clobber the test-mode
    # config (e.g. a developer's PROD-pointed env file sourced in
    # their shell). Anything we want preserved is re-set below.
    for key in list(env.keys()):
        if key.startswith("MCPOLIS_"):
            env.pop(key, None)
    env.update({
        "MCPOLIS_MODE": "cloud",
        "MCPOLIS_HOST": "127.0.0.1",
        "MCPOLIS_PORT": str(shard.backend_port),
        "MCPOLIS_MONGO_URI": f"mongodb://localhost:{TEST_MONGO_PORT}",
        "MCPOLIS_MONGO_DB_NAME": shard.db_name,
        "MCPOLIS_REDIS_URL": f"redis://localhost:{TEST_REDIS_PORT}",
        # Dev-stub auth: enables in-app email picker + the gateway's
        # test-mcp-token endpoint that helpers.ts calls for direct
        # MCP-protocol tests.
        "MCPOLIS_OAUTH_PROVIDER": "dev_stub",
        "MCPOLIS_TEST_MODE": "1",
        # Disable Sentry in the test stack. Stripping MCPOLIS_* above
        # only clears the process env; ``Settings`` still loads the
        # operator's ``backend/.env.cloud``, which may carry a real (or
        # malformed) ``MCPOLIS_SENTRY_DSN``. A blank env var overrides
        # the .env file (env > env_file in pydantic-settings), so
        # ``init_sentry`` no-ops — the test backend never phones home,
        # and a bad prod DSN can't crash shard startup.
        "MCPOLIS_SENTRY_DSN": "",
        # Fixed dev secrets (also used by the bash variant). The test
        # databases get nuked between runs so there's nothing to
        # decrypt with persistent keys.
        "MCPOLIS_SESSION_SECRET": "dev-cloud-session-secret-change-me-0123456789",
        "MCPOLIS_ENCRYPTION_KEY": "dev-cloud-encryption-key-change-me-0123456789",
        # Pin the server URL so OAuth callbacks loop back to *this*
        # shard's backend, not the developer's ngrok tunnel.
        "MCPOLIS_SERVER_URL": shard.backend_url,
        # Shrink debounce so 12-tools-list-changed doesn't sit out
        # the production 10s window.
        "MCPOLIS_POLICY_NOTIFIER_DEBOUNCE_SECONDS": "0.1",
        # NOTE: MCPOLIS_DEMO_MOUNT / MCPOLIS_DEMO_SEED are intentionally
        # NOT set. The parent env's values (if any) are stripped above
        # with the rest of the MCPOLIS_* keys, so the backend defaults
        # to off. The e2e harness uses its own per-shard
        # ``test_mcp_server.py`` instance instead — registering the
        # bundled demo would collide with shard 0's MCP server on port
        # 9999.
        # Cloud mode normally rejects stdio MCPs. Allow them so the
        # template-vars spec covering stdio command/args paths can
        # run.
        "MCPOLIS_ALLOW_STDIO_MCP": "true",
        # Plan-gate tests need a way to flip an org's subscription.
        # ``superadmin@example.com`` is a dedicated identity used by
        # the plan-gate helpers and by the seed; keeping it separate
        # from ``admin@example.com`` means the dashboard sidebar
        # rendered for ``admin@example.com`` doesn't acquire the
        # superadmin sub-tree (which would break tests that match
        # links by partial-name).
        "MCPOLIS_SUPERADMIN_EMAILS": "superadmin@example.com",
        # E2E shards seed upstreams pointing at per-shard test MCP
        # servers on 127.0.0.1 (test_mcp_server.py / oauth_test_mcp_server.py).
        # The SSRF deny-list (F-01 / url_safety.py) blocks loopback
        # by default; flip the loopback-only escape hatch on so the
        # seed can run. Prod never sets this — the strict deny-list
        # is what closes the SSRF.
        "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK": "1",
        # MCPOLIS_SANDBOX_PROVIDER is intentionally left unset. The
        # validator (entrypoints/config.py) treats an empty value as
        # "fall back to e2b when an API key is configured, else
        # local-subprocess with a warning" — and *only* rejects
        # local-subprocess when it's explicitly named. The bash
        # variant also relies on this fallback (since
        # backend/.env.cloud ships with no MCPOLIS_E2B_API_KEY).
    })
    return env


def _build_frontend_env(shard: ShardConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["MCPOLIS_BACKEND_HOST"] = "127.0.0.1"
    env["MCPOLIS_BACKEND_PORT"] = str(shard.backend_port)
    return env


def _build_demo_mcp_env(shard: ShardConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["MCPOLIS_DEMO_PORT"] = str(shard.demo_mcp_port)
    return env


def _build_oauth_mcp_env(shard: ShardConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["MCPOLIS_OAUTH_TEST_PORT"] = str(shard.oauth_mcp_port)
    return env


def start_shard(shard: ShardConfig) -> ShardProcesses:
    """Bring up the four background processes for a single shard.

    Drops the per-shard DB first so each run starts clean (the test
    mongo container has no persistent volume but the orchestrator may
    have skipped a teardown last time)."""
    drop_test_db(shard.db_name)

    log_handle = shard.log_path.open("w")

    def _spawn(label: str, argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
        # Prefix every line so interleaved subprocesses stay
        # attributable. We achieve this by piping through a sed-like
        # process — simplest cross-platform path is to let each child
        # write directly and rely on label-prefixed prefixes the
        # children themselves emit. For uvicorn / vite / starlette
        # that's not feasible without adding handlers, so instead we
        # emit a header line each time we start a child.
        log_handle.write(f"\n[{label}] starting: {' '.join(argv)} (cwd={cwd})\n")
        log_handle.flush()
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        _register(proc)
        return proc

    demo_mcp = _spawn(
        f"shard{shard.index}/demo-mcp",
        [sys.executable, str(E2E_DIR / "test_mcp_server.py")],
        cwd=REPO_ROOT,
        env=_build_demo_mcp_env(shard),
    )
    oauth_mcp = _spawn(
        f"shard{shard.index}/oauth-mcp",
        [sys.executable, str(E2E_DIR / "oauth_test_mcp_server.py")],
        cwd=REPO_ROOT,
        env=_build_oauth_mcp_env(shard),
    )

    wait_for_url(f"{shard.demo_mcp_url}/mcp", timeout=15,
                 label=f"shard{shard.index} demo MCP", proc=demo_mcp)
    wait_for_url(
        f"{shard.oauth_mcp_url}/.well-known/oauth-protected-resource",
        timeout=15,
        label=f"shard{shard.index} oauth MCP",
        proc=oauth_mcp,
    )

    backend = _spawn(
        f"shard{shard.index}/backend",
        [sys.executable, "-m", "mcpolis"],
        cwd=BACKEND_DIR,
        env=_build_backend_env(shard),
    )
    wait_for_url(f"{shard.backend_url}/health", timeout=30,
                 label=f"shard{shard.index} backend", proc=backend)

    frontend = _spawn(
        f"shard{shard.index}/frontend",
        ["npm", "run", "dev", "--", "--port", str(shard.frontend_port),
         "--strictPort"],
        cwd=FRONTEND_DIR,
        env=_build_frontend_env(shard),
    )
    wait_for_url(shard.frontend_url + "/", timeout=60,
                 label=f"shard{shard.index} frontend", proc=frontend)

    return ShardProcesses(
        backend=backend,
        frontend=frontend,
        demo_mcp=demo_mcp,
        oauth_mcp=oauth_mcp,
        log_handle=log_handle,
    )


# ─── Seeding (port of the bash dev-stub login flow) ───────────────────


def _opener_with_jar() -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        # Don't auto-follow redirects — the dev-stub flow needs to read
        # the Location header at each step.
        _NoRedirectHandler(),
    )
    return opener, jar


class _NoRedirectHandler(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):  # type: ignore[override]
        return response

    https_response = http_response  # type: ignore[assignment]


def _dev_stub_login(opener: urllib.request.OpenerDirector,
                    backend: str, email: str) -> None:
    # Step 1: GET /api/auth/login → 307 to picker with state
    req = urllib.request.Request(f"{backend}/api/auth/login")
    with opener.open(req) as resp:
        if resp.status != 307:
            raise RuntimeError(f"/api/auth/login expected 307, got {resp.status}")
        picker_loc = resp.headers["location"]

    state_match = re.search(r"[?&]state=([^&]+)", picker_loc)
    if not state_match:
        raise RuntimeError(f"could not extract state from {picker_loc}")
    state = urllib.parse.unquote(state_match.group(1))

    # Step 2: GET /api/auth/dev-stub/submit → 302 to /api/auth/callback?code=...
    submit_query = urllib.parse.urlencode({
        "email": email,
        "state": state,
        "redirect_uri": f"{backend}/api/auth/callback",
    })
    req = urllib.request.Request(f"{backend}/api/auth/dev-stub/submit?{submit_query}")
    with opener.open(req) as resp:
        if resp.status != 302:
            raise RuntimeError(f"dev-stub submit expected 302, got {resp.status}")
        callback_loc = resp.headers["location"]

    # Step 3: GET /api/auth/callback → 302 + Set-Cookie
    if callback_loc.startswith("/"):
        callback_url = backend + callback_loc
    else:
        callback_url = callback_loc
    req = urllib.request.Request(callback_url)
    with opener.open(req) as resp:
        if resp.status != 302:
            raise RuntimeError(f"callback expected 302, got {resp.status}")


def _post_json(opener: urllib.request.OpenerDirector,
               url: str, body: dict[str, Any]) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _post_empty(opener: urllib.request.OpenerDirector,
                url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, data=b"", method="POST")
    with opener.open(req) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _patch_json(opener: urllib.request.OpenerDirector,
                url: str, body: dict[str, Any]) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _get_json(opener: urllib.request.OpenerDirector, url: str) -> Any:
    with opener.open(urllib.request.Request(url)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def seed_shard(shard: ShardConfig) -> None:
    """Seed the shard's database with the canonical test fixtures.

    Mirrors the seeding section of the original run-e2e-tests.sh —
    org acme-corp, two admins + one regular user, three upstreams
    (test-tools service_account, oauth-tools admin_oauth, oauth-tools-pu
    per_user_oauth). Uses urllib + a cookie jar to walk the dev-stub
    OAuth flow exactly like the browser does."""
    backend = shard.backend_url
    opener, _jar = _opener_with_jar()

    # Login as the org's admin and create the org. After the org is
    # created we re-login so the new session cookie picks up the
    # ``org_slug`` claim.
    _dev_stub_login(opener, backend, "admin@example.com")
    _post_json(
        opener, f"{backend}/api/orgs",
        {"slug": "acme-corp", "display_name": "Acme Corp"},
    )
    _dev_stub_login(opener, backend, "admin@example.com")
    # Flip the seeded org to ``team`` so the existing e2e suites,
    # which freely create users + upstreams + roles, aren't gated by
    # the Free-tier caps. The dedicated ``34-plan-gates.spec.ts``
    # flips back to ``free`` per-test for the gate assertions.
    # Switch to a dedicated superadmin opener so the dashboard test
    # admin's sidebar isn't polluted with /superadmin links.
    sa_opener, _ = _opener_with_jar()
    _dev_stub_login(sa_opener, backend, "superadmin@example.com")
    orgs_resp = _get_json(sa_opener, f"{backend}/api/superadmin/orgs")
    org_row = next(
        (o for o in orgs_resp.get("orgs", []) if o.get("slug") == "acme-corp"),
        None,
    )
    if org_row is None:
        raise RuntimeError("seeded org acme-corp missing from superadmin list")
    _patch_json(
        sa_opener,
        f"{backend}/api/superadmin/orgs/{org_row['id']}/subscription",
        {"plan": "team"},
    )

    _post_json(
        opener, f"{backend}/api/admin/users",
        {"email": "alice@example.com", "role": "user"},
    )
    _post_json(
        opener, f"{backend}/api/admin/users",
        {"email": "admin2@example.com", "role": "admin"},
    )

    _post_json(
        opener, f"{backend}/api/admin/upstreams",
        {
            "id": "test-tools",
            "display_name": "Test Tools",
            "url": f"{shard.demo_mcp_url}/mcp",
            "auth_mode": "service_account",
        },
    )
    _post_empty(
        opener, f"{backend}/api/admin/upstreams/test-tools/reconnect",
    )

    _post_json(
        opener, f"{backend}/api/admin/upstreams",
        {
            "id": "oauth-tools",
            "display_name": "OAuth Tools",
            "url": f"{shard.oauth_mcp_url}/mcp",
            "auth_mode": "admin_oauth",
            "client_id": "e2e-client",
            "client_secret": "e2e-secret",
        },
    )

    _post_json(
        opener, f"{backend}/api/admin/upstreams",
        {
            "id": "oauth-tools-pu",
            "display_name": "OAuth Tools (per-user)",
            "url": f"{shard.oauth_mcp_url}/mcp",
            "auth_mode": "per_user_oauth",
            "client_id": "e2e-client",
            "client_secret": "e2e-secret",
        },
    )


# ─── Bin-packing partitioner ──────────────────────────────────────────


def discover_spec_files() -> list[str]:
    """Return all ``*.spec.ts`` filenames (basename only) under
    tests/e2e/, sorted for stability."""
    return sorted(p.name for p in E2E_DIR.glob("*.spec.ts"))


def split_passthru(passthru: list[str]) -> tuple[list[str], list[str]]:
    """Separate file references (``foo.spec.ts``) from flag-style args
    (``-g``, ``--grep=...``, etc.) in the user's passthru. The first
    element is the file list (if any); the rest stay as flags.

    Heuristic: an arg is considered a file reference if it ends in
    ``.spec.ts`` and exists under E2E_DIR. Anything starting with ``-``
    or that doesn't match a known spec is treated as a flag (and stays
    intact for Playwright to interpret)."""
    files: list[str] = []
    flags: list[str] = []
    known_specs = set(discover_spec_files())
    skip_next = False
    for arg in passthru:
        if skip_next:
            flags.append(arg)
            skip_next = False
            continue
        if arg.startswith("-"):
            flags.append(arg)
            # ``-g pattern`` form: the next token is the value, not a
            # spec name. Keep them paired.
            if arg in {"-g", "--grep", "--grep-invert"}:
                skip_next = True
            continue
        # Bare positional: only treat as a file when it actually maps
        # to a known spec; otherwise fall through to flags so Playwright
        # surfaces a clearer "no tests found" error.
        if arg.endswith(".spec.ts") and arg in known_specs:
            files.append(arg)
        else:
            flags.append(arg)
    return files, flags


def load_spec_times() -> dict[str, float]:
    """Load the per-spec duration cache. Missing or corrupt → empty."""
    if not SPEC_TIMES_CACHE.exists():
        return {}
    try:
        raw = json.loads(SPEC_TIMES_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    items: list[tuple[Any, Any]] = list(raw.items())  # type: ignore[arg-type]
    for k, v in items:
        if isinstance(k, str) and isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def save_spec_times(times: dict[str, float]) -> None:
    SPEC_TIMES_CACHE.write_text(json.dumps(times, indent=2, sort_keys=True))


def bin_pack(files: list[str], times: dict[str, float],
             n_shards: int) -> list[list[str]]:
    """Longest-processing-time-first first-fit partition.

    For each spec, look up its cached duration (defaulting to the
    median of known durations, or 1.0 if the cache is empty). Sort
    descending, then assign each spec to whichever shard currently
    has the smallest accumulated duration. Returns a list of N
    file-name lists, indexed by shard."""
    if n_shards <= 1:
        return [list(files)]

    # Median of known durations as the fallback for unknown specs —
    # avoids a massive new spec being treated as zero-cost (would all
    # land on shard 0) or as huge (would dominate the partition).
    known_durations = [times[f] for f in files if f in times]
    if known_durations:
        sorted_durs = sorted(known_durations)
        median = sorted_durs[len(sorted_durs) // 2]
    else:
        median = 1.0

    weighted = [(f, times.get(f, median)) for f in files]
    weighted.sort(key=lambda x: -x[1])

    bins: list[list[str]] = [[] for _ in range(n_shards)]
    bin_loads: list[float] = [0.0] * n_shards
    for fname, weight in weighted:
        target = min(range(n_shards), key=lambda i: bin_loads[i])
        bins[target].append(fname)
        bin_loads[target] += weight

    # Re-sort each bin alphabetically for deterministic, grep-able
    # logs. The order within a bin doesn't affect correctness.
    for b in bins:
        b.sort()
    return bins


def update_spec_times_from_reports(shards: list[ShardConfig]) -> None:
    """After a run, walk each shard's Playwright JSON report and
    record the *measured* per-spec duration. Merge into the cache so
    the next run starts with fresher numbers."""
    times = load_spec_times()
    for shard in shards:
        if not shard.json_report_path.exists():
            continue
        try:
            data = json.loads(shard.json_report_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # Walk suites: the top-level "suites" entries each correspond
        # to a spec file. Each one's "specs[*].tests[*].results[*].duration"
        # in ms — sum across to get the file's wall-clock contribution.
        for top in data.get("suites", []):
            file_field = top.get("file") or top.get("title")
            if not isinstance(file_field, str):
                continue
            total_ms = _sum_durations(top)
            if total_ms <= 0:
                continue
            # Use only the basename so the cache key is stable across
            # working directories.
            key = Path(file_field).name
            times[key] = total_ms / 1000.0
    # Garbage-collect entries whose spec file no longer exists.
    live = set(discover_spec_files())
    times = {k: v for k, v in times.items() if k in live}
    save_spec_times(times)


def _sum_durations(node: dict[str, Any]) -> int:
    total = 0
    for sp in node.get("specs", []):
        for t in sp.get("tests", []):
            for r in t.get("results", []):
                d = r.get("duration", 0)
                if isinstance(d, (int, float)):
                    total += int(d)
    for child in node.get("suites", []):
        total += _sum_durations(child)
    return total


# ─── Playwright execution ─────────────────────────────────────────────


def run_playwright(shard: ShardConfig, passthru: list[str],
                   spec_files: list[str] | None) -> int:
    """Run Playwright for a single shard, writing output to the shard's
    log file. Returns the process exit code.

    ``spec_files`` is the smart-partitioned list of files this shard
    should run. When ``None`` (single-shard mode or no partition data),
    Playwright's own discovery + ``--shard k/N`` flag is used."""
    log_handle = shard.log_path.open("a")
    log_handle.write(f"\n[shard{shard.index}/playwright] starting "
                     f"({shard.index + 1}/{shard.total})\n")
    if spec_files is not None:
        log_handle.write(
            f"[shard{shard.index}/playwright] specs: "
            f"{', '.join(spec_files) if spec_files else '(none)'}\n",
        )
    log_handle.flush()

    env = os.environ.copy()
    env.update({
        "E2E_BASE_URL": shard.frontend_url,
        "E2E_BACKEND_URL": shard.backend_url,
        "E2E_TEST_MCP_URL": shard.demo_mcp_url,
        "E2E_OAUTH_TEST_MCP_URL": shard.oauth_mcp_url,
    })

    argv = [
        "npx", "playwright", "test",
        "--project=chromium",
        "--reporter=list,json",
    ]
    # Playwright's JSON reporter target is set via env var, not CLI.
    env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(shard.json_report_path)

    if spec_files is not None:
        # Smart partition mode: pass an explicit file list. Playwright
        # runs only the named files plus any further filtering from
        # passthru (-g, --grep, etc.).
        if not spec_files:
            # This shard has nothing to do — exit cleanly without
            # spawning Playwright (which would 1-error on an empty
            # globbed test list anyway).
            log_handle.write(
                f"[shard{shard.index}/playwright] no specs assigned, skipping\n",
            )
            log_handle.close()
            # Touch the JSON report so aggregation has a stable file.
            shard.json_report_path.write_text(
                json.dumps({"stats": {"expected": 0, "unexpected": 0,
                                       "flaky": 0, "skipped": 0,
                                       "duration": 0},
                            "suites": []}),
            )
            return 0
        argv.extend(spec_files)
        argv.extend(passthru)
    elif shard.total > 1:
        # Fallback: dumb hash-based sharding.
        argv.append(f"--shard={shard.index + 1}/{shard.total}")
        argv.extend(passthru)
    else:
        argv.extend(passthru)

    proc = subprocess.Popen(
        argv,
        cwd=str(E2E_DIR),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    exit_code = proc.wait()
    log_handle.close()
    return exit_code


# ─── Aggregation ──────────────────────────────────────────────────────


def _empty_dict_list() -> list[dict[str, Any]]:
    return []


@dataclass
class AggregateResult:
    total_specs: int = 0
    passed: int = 0
    failed: int = 0
    flaky: int = 0
    skipped: int = 0
    failures: list[dict[str, Any]] = field(default_factory=_empty_dict_list)
    shards: list[dict[str, Any]] = field(default_factory=_empty_dict_list)


def aggregate_reports(shards: list[ShardConfig],
                      exit_codes: dict[int, int]) -> AggregateResult:
    """Read each shard's Playwright JSON report and produce a unified
    summary."""
    agg = AggregateResult()
    for shard in shards:
        shard_summary: dict[str, Any] = {
            "shard": shard.index,
            "exit_code": exit_codes.get(shard.index, -1),
            "report_present": shard.json_report_path.exists(),
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "duration_ms": 0,
        }
        if not shard.json_report_path.exists():
            agg.shards.append(shard_summary)
            continue
        try:
            data = json.loads(shard.json_report_path.read_text())
        except json.JSONDecodeError as e:
            shard_summary["parse_error"] = str(e)
            agg.shards.append(shard_summary)
            continue

        stats = data.get("stats", {})
        # Playwright JSON: { stats: {expected, unexpected, skipped, ...},
        #                    suites: [...] }
        passed = int(stats.get("expected", 0))
        failed = int(stats.get("unexpected", 0))
        flaky = int(stats.get("flaky", 0))
        skipped = int(stats.get("skipped", 0))
        duration = int(stats.get("duration", 0))

        agg.passed += passed
        agg.failed += failed
        agg.flaky += flaky
        agg.skipped += skipped
        agg.total_specs += passed + failed + skipped
        shard_summary.update(
            passed=passed, failed=failed, skipped=skipped,
            duration_ms=duration,
        )
        agg.shards.append(shard_summary)

        # Walk the suite tree to collect failure titles + the spec file
        # they came from. Keeps the aggregate JSON small enough to grep.
        for suite in data.get("suites", []):
            _collect_failures(suite, [], shard.index, agg.failures)
    return agg


def _collect_failures(node: dict[str, Any], path: list[str],
                      shard_index: int,
                      out: list[dict[str, Any]]) -> None:
    title = node.get("title")
    new_path = path + ([title] if title else [])
    for spec in node.get("specs", []):
        spec_title = spec.get("title", "<no-title>")
        spec_file = spec.get("file") or node.get("file") or "<unknown>"
        for test in spec.get("tests", []):
            for result in test.get("results", []):
                if result.get("status") in ("failed", "timedOut"):
                    out.append({
                        "shard": shard_index,
                        "file": spec_file,
                        "describe": " > ".join(new_path),
                        "title": spec_title,
                        "status": result.get("status"),
                        "error": _truncate(result.get("error", {}).get("message", ""), 800),
                    })
    for child in node.get("suites", []):
        _collect_failures(child, new_path, shard_index, out)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "...[truncated]"


def write_aggregate(agg: AggregateResult) -> None:
    json_path = Path("/tmp/mcpolis-e2e-aggregate.json")
    txt_path = Path("/tmp/mcpolis-e2e-aggregate.txt")
    json_path.write_text(json.dumps({
        "passed": agg.passed,
        "failed": agg.failed,
        "flaky": agg.flaky,
        "skipped": agg.skipped,
        "total_specs": agg.total_specs,
        "shards": agg.shards,
        "failures": agg.failures,
    }, indent=2))
    lines = [
        "═" * 60,
        f"E2E aggregate: {agg.passed} passed, {agg.failed} failed, "
        f"{agg.flaky} flaky, {agg.skipped} skipped",
        "─" * 60,
    ]
    for s in agg.shards:
        lines.append(
            f"  shard {s['shard']}: passed={s['passed']} failed={s['failed']} "
            f"skipped={s['skipped']} exit={s['exit_code']} "
            f"({s['duration_ms']/1000:.1f}s)"
        )
    if agg.failures:
        lines.append("─" * 60)
        lines.append("Failures:")
        for f in agg.failures:
            lines.append(f"  [shard {f['shard']}] {f['file']}")
            lines.append(f"    {f['describe']} > {f['title']}  ({f['status']})")
    lines.append("═" * 60)
    txt = "\n".join(lines) + "\n"
    txt_path.write_text(txt)
    print(txt, end="")


# ─── Main orchestration ───────────────────────────────────────────────


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--shards", type=int, default=1,
        help="Number of parallel shards (each is a fully-independent "
             "backend stack). Default: 1.",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Tear down test mongo / redis containers on exit "
             "(default: leave them up for fast rerun).",
    )
    parser.add_argument(
        "--keep-going", action="store_true",
        help="Don't abort the run when one shard fails to bootstrap; "
             "let other shards finish. Exit code reflects any failure.",
    )
    return parser.parse_known_args(argv)


def _bootstrap_shard(shard: ShardConfig) -> tuple[ShardConfig, str | None]:
    try:
        start_shard(shard)
        seed_shard(shard)
        return shard, None
    except Exception as e:
        return shard, f"{type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    args, passthru = parse_args(argv if argv is not None else sys.argv[1:])
    if args.shards < 1:
        print("--shards must be ≥ 1", file=sys.stderr)
        return 2
    # Pre-flight: clear any leaked e2e children from an interrupted
    # prior run before we probe for ports, so this run lands on the
    # predictable preferred ports rather than spilling around a
    # squatter. Safe by construction — never touches the dev stack or
    # a live concurrent run (see ``reap_leaked_e2e_orphans``).
    reap_leaked_e2e_orphans(args.shards)
    shards = [make_shard(i, args.shards) for i in range(args.shards)]

    # Wipe stale per-shard outputs so an aborted previous run can't
    # contaminate the aggregate.
    for shard in shards:
        for p in (shard.log_path, shard.json_report_path):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
    for p in (Path("/tmp/mcpolis-e2e-aggregate.json"),
              Path("/tmp/mcpolis-e2e-aggregate.txt")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()

    def _signal_handler(sig: int, _frame: Any) -> None:
        print(f"\n[orchestrator] received signal {sig}, tearing down...")
        _terminate_all()
        sys.exit(130)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    overall_exit = 0
    try:
        ensure_test_infra()

        print(f"[orchestrator] bootstrapping {len(shards)} shard(s) in parallel...")
        bootstrap_failures: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(shards))) as ex:
            futures = {ex.submit(_bootstrap_shard, s): s for s in shards}
            for fut in as_completed(futures):
                shard, err = fut.result()
                if err:
                    bootstrap_failures.append((shard.index, err))
                    print(f"[shard {shard.index}] BOOTSTRAP FAILED: {err}")
                else:
                    print(f"[shard {shard.index}] ready "
                          f"(backend:{shard.backend_port} frontend:{shard.frontend_port} "
                          f"demo:{shard.demo_mcp_port} oauth:{shard.oauth_mcp_port})")
        if bootstrap_failures and not args.keep_going:
            return 1

        # Bin-pack specs into per-shard file lists. Falls back to
        # Playwright's hash-based ``--shard k/N`` only when the user
        # passes filter flags we can't pre-evaluate (e.g. ``-g``); a
        # plain run uses the smart partition.
        passthru_files, passthru_flags = split_passthru(passthru)
        partitions: list[list[str]] | None = None
        if args.shards > 1:
            if passthru_files:
                pool = passthru_files
            else:
                pool = discover_spec_files()
            spec_times = load_spec_times()
            partitions = bin_pack(pool, spec_times, args.shards)
            for i, b in enumerate(partitions):
                est = sum(spec_times.get(f, 0.0) for f in b)
                print(f"[partition] shard {i}: {len(b)} specs, "
                      f"est ~{est:.1f}s ({', '.join(b) if b else '∅'})")

        print(f"[orchestrator] running playwright across {len(shards)} shard(s)...")
        exit_codes: dict[int, int] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(shards))) as ex:
            futures: dict[Any, ShardConfig] = {}
            for s in shards:
                spec_files = partitions[s.index] if partitions is not None else None
                # When the user passed flags but no files, the partition
                # has all known specs and the flags still need to flow
                # through (e.g. ``-g pattern``).
                shard_passthru = passthru_flags if partitions is not None else passthru
                fut = ex.submit(run_playwright, s, shard_passthru, spec_files)
                futures[fut] = s
            for fut in as_completed(futures):
                shard = futures[fut]
                code = fut.result()
                exit_codes[shard.index] = code
                if code != 0:
                    overall_exit = code
                print(f"[shard {shard.index}] playwright exited {code}")

        agg = aggregate_reports(shards, exit_codes)
        write_aggregate(agg)
        update_spec_times_from_reports(shards)
        if agg.failed > 0:
            overall_exit = overall_exit or 1
        return overall_exit

    finally:
        _terminate_all()
        for shard in shards:
            drop_test_db(shard.db_name)
        # Best-effort: free *all* shard ports for the next run, not just
        # the backend. A uvicorn/vite/MCP child can hold its port a
        # moment after SIGTERM, and a leftover vite (15xxx) squatting
        # into the next run was a prime cause of the cross-run cascade.
        for shard in shards:
            for port in (shard.backend_port, shard.frontend_port,
                         shard.demo_mcp_port, shard.oauth_mcp_port):
                wait_for_port_free(port, timeout=2)
        if args.clean:
            teardown_test_infra()


if __name__ == "__main__":
    sys.exit(main())
