"""Diagnostic: what actually breaks on the first call after an E2B
sandbox wakes from pause?

Production symptom (Sentry ``MCPOLIS-BACKEND-16``): the elasticsearch
stdio MCP answers the first ``tools/call`` after a wake with
``error sending request for url (...)`` and succeeds on the next
one. Across 2026-07-07/08 and 2026-09-19/20, 19 of 19 connect-class
failures sit on a first-call-after-wake and none sit anywhere else,
so the *when* is settled. The *why* is not: ``reqwest``'s Display
drops the error source, so the production logs never carry the
underlying ``ECONNRESET`` / ``EAI_AGAIN`` / ``EHOSTUNREACH``.

Two explanations fit the logs equally well:

  A. the sandbox's own egress is not up yet at resume;
  B. the long-lived MCP process carries a dead pooled socket (and a
     stale DNS answer) across the freeze and writes into it.

They need different fixes, so this script separates them. Each cycle
pauses the sandbox, resumes it exactly the way production does
(``commands.connect(pid)``, which triggers E2B's ``auto_resume``),
then fires two probes CONCURRENTLY at that same instant:

  FRESH   a brand-new ``node`` process: no pooled socket, no cached
          DNS, full resolve + TCP + TLS. Failing here means (A).
  POOLED  the long-lived process reusing its keep-alive socket,
          which is what the real MCP server does. Failing here while
          FRESH passes means (B).

Deliberately stricter than production: ``set_timeout`` is NOT
re-applied between the resume and the probes, so the probes hit the
earliest moment after the wake that the SDK allows. A FRESH pass
under that timing is the strongest available evidence against (A).

Run:
    export MCPOLIS_E2B_API_KEY=...
    python backend/tests/integration/diagnose_wake_network.py

Knobs (env):
    WAKE_CYCLES          number of pause/resume cycles (default 12)
    WAKE_PAUSE_SECONDS   sleep while paused (default 60)
    WAKE_TARGET_URL      probe target (default Google's 204 endpoint)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import cast

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mcpolis.adapters.sandbox_e2b import (  # noqa: E402
    E2BProcessHandle,
    E2BSandboxHandle,
    RealE2BClient,
)
from mcpolis.adapters.sandbox_e2b.template_grid import (  # noqa: E402
    template_name_for,
)

API_KEY = os.environ.get("MCPOLIS_E2B_API_KEY") or os.environ.get("E2B_API_KEY")
RUN_ID = uuid.uuid4().hex[:8]

CYCLES = int(os.environ.get("WAKE_CYCLES", "12"))
PAUSE_SECONDS = float(os.environ.get("WAKE_PAUSE_SECONDS", "60"))
TARGET_URL = os.environ.get(
    "WAKE_TARGET_URL", "https://www.google.com/generate_204",
)
# How many keep-alive sockets the long-lived process holds open when
# the freeze lands. 1 reproduces the minimal case; >1 answers the
# question a single retry cannot: does the client hand out a SECOND
# dead socket after the first one resets? A real MCP server's HTTP
# pool is not capped at one.
POOL_SOCKETS = int(os.environ.get("WAKE_POOL_SOCKETS", "1"))
# Consecutive post-wake attempts, to find how many retries it actually
# takes before one succeeds.
MAX_ATTEMPTS = int(os.environ.get("WAKE_MAX_ATTEMPTS", "5"))

# Generous: the whole point is to distinguish a fast failure from a
# slow one, so neither probe may be cut short by our own deadline.
PROBE_TIMEOUT_SECONDS = 45.0
# The sandbox is paused explicitly, so the idle timer never matters;
# keep it comfortably above one cycle anyway.
SANDBOX_TIMEOUT_SECONDS = 900

PROBE_PATH = "/home/user/probe.js"
FRESH_PATH = "/home/user/fresh.js"

# Long-lived process: one keep-alive agent, one socket, reused for
# every request. ``keepAliveMsecs`` is pushed far out so the socket is
# still pooled after the freeze rather than being reaped by our own
# timer. ``req.reusedSocket`` tells us whether the request actually
# went down the old socket, which is what makes a POOLED failure
# meaningful rather than incidental.
PROBE_JS = r"""
const https = require('node:https');
const TARGET = process.env.TARGET_URL;
const POOL = parseInt(process.env.POOL_SOCKETS || '1', 10);
const agent = new https.Agent({
  keepAlive: true,
  keepAliveMsecs: 600000,
  maxSockets: POOL,
});

function pooledCount() {
  let n = 0;
  for (const key of Object.keys(agent.freeSockets || {})) {
    n += (agent.freeSockets[key] || []).length;
  }
  return n;
}

function emit(obj) {
  process.stdout.write('RESULT ' + JSON.stringify(obj) + '\n');
}

function fireOnce(tag) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const pooledBefore = pooledCount();
    let req;
    try {
      req = https.request(TARGET, { agent, method: 'GET' }, (res) => {
        res.resume();
        res.on('end', () => {
          resolve({
            tag, ok: true, status: res.statusCode,
            reused: req.reusedSocket === true,
            pooledBefore, ms: Date.now() - t0,
          });
        });
      });
    } catch (e) {
      resolve({
        tag, ok: false, reused: false, pooledBefore, ms: Date.now() - t0,
        code: e.code || null, msg: String(e.message || e),
      });
      return;
    }
    req.on('error', (e) => {
      resolve({
        tag, ok: false, reused: req.reusedSocket === true, pooledBefore,
        ms: Date.now() - t0, code: e.code || null,
        syscall: e.syscall || null,
        errno: (e.errno === undefined ? null : e.errno),
        msg: String(e.message || e),
      });
    });
    req.end();
  });
}

// ``warm N`` fires N requests at once so the agent ends up holding N
// idle sockets, which is the state the freeze then captures. Sockets
// rejoin freeSockets on the next tick, hence the short settle.
function warm(n) {
  const jobs = [];
  for (let i = 0; i < n; i++) jobs.push(fireOnce('warm'));
  Promise.all(jobs).then((rs) => {
    setTimeout(() => {
      emit({
        tag: 'warm', ok: rs.every((r) => r.ok), asked: n,
        pooledAfter: pooledCount(),
        codes: rs.map((r) => (r.ok ? 'ok' : (r.code || 'ERR'))),
      });
    }, 100);
  });
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    const parts = line.split(/\s+/);
    if (parts[0] === 'warm') {
      warm(parseInt(parts[1] || '1', 10));
    } else {
      fireOnce('pooled').then(emit);
    }
  }
});
process.stdout.write('READY\n');
"""

# Brand-new process each time: default agent, no keep-alive, so this
# is a full DNS resolve + TCP connect + TLS handshake from scratch.
FRESH_JS = r"""
const https = require('node:https');
const TARGET = process.env.TARGET_URL;
const t0 = Date.now();
function emit(obj) {
  process.stdout.write('RESULT ' + JSON.stringify(obj) + '\n');
}
const req = https.request(TARGET, { method: 'GET' }, (res) => {
  res.resume();
  res.on('end', () => {
    emit({ tag: 'fresh', ok: true, status: res.statusCode, ms: Date.now() - t0 });
    process.exit(0);
  });
});
req.on('error', (e) => {
  emit({
    tag: 'fresh', ok: false, ms: Date.now() - t0, code: e.code || null,
    syscall: e.syscall || null, errno: (e.errno === undefined ? null : e.errno),
    msg: String(e.message || e),
  });
  process.exit(0);
});
req.end();
"""


class LineSink:
    """Collects newline-delimited stdout from a sandbox process.

    Survives a reattach: the reattached handle simply feeds the same
    sink, so a cycle's result lands in the same queue regardless of
    which handle delivered it.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._buf = ""

    def feed(self, data: bytes) -> None:
        self._buf += data.decode("utf-8", errors="replace")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self.queue.put_nowait(line)

    def drain(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    async def next_result(
        self, timeout: float,
    ) -> dict[str, object] | None:
        """Return the next ``RESULT {...}`` payload, or None on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                line = await asyncio.wait_for(
                    self.queue.get(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                return None
            if line.startswith("RESULT "):
                try:
                    parsed: object = json.loads(line[len("RESULT "):])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return cast("dict[str, object]", parsed)

    async def wait_for_line(
        self, needle: str, timeout: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                line = await asyncio.wait_for(
                    self.queue.get(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                return False
            if needle in line:
                return True


def describe(result: dict[str, object] | None) -> str:
    if result is None:
        return "NO ANSWER (stdout silent)"
    if result.get("ok") is True:
        bits = [f"ok {result.get('status')}", f"{result.get('ms')}ms"]
        if result.get("reused") is True:
            bits.append("reused socket")
        elif "reused" in result:
            bits.append("new socket")
        return " · ".join(bits)
    bits = [
        f"FAIL {result.get('code') or '?'}",
        f"{result.get('ms')}ms",
    ]
    if result.get("syscall"):
        bits.append(f"syscall={result.get('syscall')}")
    if result.get("reused") is True:
        bits.append("reused socket")
    elif "reused" in result:
        bits.append("new socket")
    bits.append(str(result.get("msg", "")))
    return " · ".join(bits)


async def run_fresh_probe(
    sandbox: E2BSandboxHandle,
) -> dict[str, object] | None:
    """Spawn a brand-new node process and read its single result."""
    sink = LineSink()
    handle = await sandbox.run_command(
        ["node", FRESH_PATH],
        env={"TARGET_URL": TARGET_URL},
        on_stdout=sink.feed,
        on_stderr=lambda _data: None,
    )
    try:
        return await sink.next_result(PROBE_TIMEOUT_SECONDS)
    finally:
        try:
            await handle.release()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


async def send_probe(
    process: E2BProcessHandle, sink: LineSink, command: bytes,
) -> dict[str, object] | None:
    try:
        await process.send_stdin(command)
    except Exception as exc:  # noqa: BLE001 - report, don't abort the run
        return {"ok": False, "code": "STDIN_FAILED", "ms": 0, "msg": str(exc)}
    return await sink.next_result(PROBE_TIMEOUT_SECONDS)


async def run_pooled_probe(
    process: E2BProcessHandle, sink: LineSink,
) -> dict[str, object] | None:
    """Ask the long-lived process to reuse its pooled socket, once."""
    return await send_probe(process, sink, b"pooled\n")


async def run_warmup(
    process: E2BProcessHandle, sink: LineSink, sockets: int,
) -> dict[str, object] | None:
    """Fill the keep-alive pool so the freeze captures ``sockets`` of them."""
    return await send_probe(
        process, sink, f"warm {sockets}\n".encode(),
    )


async def run_retry_ladder(
    process: E2BProcessHandle, sink: LineSink,
) -> list[dict[str, object] | None]:
    """Fire sequential pooled calls until one succeeds.

    The length of the returned list is what a retry policy would have
    to tolerate: 1 means the first call worked, 2 means one retry was
    enough, 3+ means a single retry is NOT enough.
    """
    attempts: list[dict[str, object] | None] = []
    for _ in range(MAX_ATTEMPTS):
        result = await run_pooled_probe(process, sink)
        attempts.append(result)
        if result is not None and result.get("ok") is True:
            break
    return attempts


async def main() -> int:
    if not API_KEY:
        print("MCPOLIS_E2B_API_KEY (or E2B_API_KEY) required", file=sys.stderr)
        return 2

    template = template_name_for(language="node", cpu_vcpus=1, memory_mb=1024)
    print(f"run {RUN_ID} · template {template} · target {TARGET_URL}")
    print(
        f"{CYCLES} cycles · {PAUSE_SECONDS:.0f}s paused each · "
        "probes fire concurrently, immediately after resume",
    )
    print()

    client = RealE2BClient(api_key=API_KEY)
    sandbox = await client.create_sandbox(
        template=template,
        metadata={
            "mcpolis_instance": f"diag-wake-{RUN_ID}",
            "mcpolis_purpose": "wake-network-diagnostic",
        },
        timeout_seconds=SANDBOX_TIMEOUT_SECONDS,
    )
    print(f"sandbox {sandbox.sandbox_id} created")

    rows: list[tuple[int, str, str, float]] = []
    ladder_counts: list[int] = []
    sink = LineSink()
    process: E2BProcessHandle | None = None
    try:
        await sandbox.write_file(path=PROBE_PATH, contents=PROBE_JS, mode=0o644)
        await sandbox.write_file(path=FRESH_PATH, contents=FRESH_JS, mode=0o644)

        process = await sandbox.run_command(
            ["node", PROBE_PATH],
            env={"TARGET_URL": TARGET_URL, "POOL_SOCKETS": str(POOL_SOCKETS)},
            on_stdout=sink.feed,
            on_stderr=lambda _data: None,
        )
        if not await sink.wait_for_line("READY", timeout=30.0):
            print("probe process never printed READY", file=sys.stderr)
            return 1
        print(f"long-lived probe running as pid {process.pid}")
        print()

        for cycle in range(1, CYCLES + 1):
            # Warm-up: leaves POOL_SOCKETS idle sockets in the keep-alive
            # pool, which is the state the real MCP server is frozen in.
            sink.drain()
            warm = await run_warmup(process, sink, POOL_SOCKETS)
            if warm is None or warm.get("ok") is not True:
                print(f"cycle {cycle}: warm-up failed ({describe(warm)}), skipping")
                continue
            pooled_at_freeze = warm.get("pooledAfter")

            await sandbox.pause()
            await asyncio.sleep(PAUSE_SECONDS)

            # Production's exact resume path: commands.connect(pid)
            # triggers auto_resume. Nothing else runs before the probes.
            sink.drain()
            resume_start = time.monotonic()
            old_process = process
            process = await sandbox.connect_command(
                pid=old_process.pid,
                on_stdout=sink.feed,
                on_stderr=lambda _data: None,
            )
            resume_ms = (time.monotonic() - resume_start) * 1000.0

            fresh_result, ladder = await asyncio.gather(
                run_fresh_probe(sandbox),
                run_retry_ladder(process, sink),
            )

            try:
                await old_process.release()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass

            fresh_text = describe(fresh_result)
            pooled_text = describe(ladder[0] if ladder else None)
            rows.append((cycle, fresh_text, pooled_text, resume_ms))
            attempts_needed = len(ladder)
            ladder_counts.append(attempts_needed)
            print(
                f"cycle {cycle:2d}  resume {resume_ms:7.0f}ms  "
                f"pooled sockets at freeze {pooled_at_freeze}",
            )
            print(f"          FRESH   {fresh_text}")
            for index, attempt in enumerate(ladder, start=1):
                print(f"          POOLED#{index} {describe(attempt)}")
    finally:
        if process is not None:
            try:
                await process.release()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        try:
            await sandbox.kill()
            print(f"\nsandbox {sandbox.sandbox_id} killed")
        except Exception as exc:  # noqa: BLE001 - report, don't mask results
            print(f"\nsandbox cleanup failed: {exc}", file=sys.stderr)

    if not rows:
        print("no cycles completed", file=sys.stderr)
        return 1

    fresh_fail = sum(1 for _c, f, _p, _r in rows if not f.startswith("ok"))
    pooled_fail = sum(1 for _c, _f, p, _r in rows if p.startswith("FAIL"))
    pooled_silent = sum(1 for _c, _f, p, _r in rows if p.startswith("NO ANSWER"))

    worst_ladder = max(ladder_counts) if ladder_counts else 0
    needed_more_than_one_retry = sum(1 for n in ladder_counts if n > 2)

    print()
    print("=" * 72)
    print(f"cycles completed     {len(rows)}")
    print(f"pool sockets frozen  {POOL_SOCKETS}")
    print(f"FRESH failed         {fresh_fail}")
    print(f"POOLED failed        {pooled_fail}")
    print(f"POOLED no answer     {pooled_silent}  (the separate stdout-stall bug)")
    print(f"attempts to succeed  {ladder_counts}")
    print(f"worst case           {worst_ladder} attempts")
    print(
        f"cycles ONE retry would NOT have fixed: {needed_more_than_one_retry}",
    )
    print("=" * 72)

    if fresh_fail == 0 and pooled_fail == 0:
        print("VERDICT  not reproduced. Neither arm failed.")
    elif fresh_fail == 0 and pooled_fail > 0:
        print(
            "VERDICT  (B) stale state inside the frozen process. The sandbox "
            "network was up every time; only the reused socket broke.",
        )
    elif fresh_fail > 0 and pooled_fail == 0:
        print(
            "VERDICT  (A) sandbox egress not ready at resume. A brand-new "
            "process could not reach the network either.",
        )
    else:
        print(
            "VERDICT  mixed. Both arms failed; read the per-cycle error codes "
            "above to see whether they share a cause.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
