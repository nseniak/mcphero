"""Real-SDK gate: a sandbox in use is never paused; an idle one is.

E2B pauses a sandbox a fixed time after the last ``set_timeout``,
whatever the traffic (measured 2026-10-01: a 20 s timeout paused 20.1 s
after creation with a line going through the process every 4 s). The
service used to call it only when a session opened, so in production a
user calling tools the whole time hit a pause every 60 s, and a call
still running at the pause was cut off. ``IdlePauseTimer`` now re-arms
the timer on MCP traffic.

One sandbox, three phases, so the whole check costs one sandbox for
about four minutes (well under one cent):

1. a call every 20 s for 2+ minutes, against a 30 s window: no pause;
2. one call that takes 45 s, with no other traffic: no pause;
3. silence from the caller while the server keeps talking on its own
   (answers to nothing, "list changed" notices): the sandbox still
   pauses, no sooner than the window after the last answer, and no
   later than the window plus the refresh gap.

The server also echoes each tool call's id as text ("7" for 7), which
the MCP client accepts. A timer that failed to match those answers
would keep every call "pending" and the sandbox awake for minutes.

The test drives a raw ``ClientSession`` on purpose. Through the
gateway, the liveness ping every 30 s would add traffic of its own
during phase 2 and hide whether a pending call alone keeps the timer
armed.

To run::

    bash backend/run-integration-tests.sh \
        tests/integration/test_e2b_idle_pause_e2e.py -v -s
"""
from __future__ import annotations

import asyncio
import time
from io import StringIO
from typing import cast

import pytest
from mcp import types as mcp_types
from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.idle_pause_timer import MIN_REFRESH_GAP_SECONDS
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.domain.model.upstream import UpstreamDefinition
from tests.integration._e2b_broad_matrix_helpers import (
    E2B_API_KEY,
    IDLE_PAUSE_SECONDS,
    INITIALIZE_TIMEOUT,
    TEST_RUN_ID,
    is_template_missing_error,
    make_resources,
    make_service,
    make_test_client,
    sweep_kill,
)
from tests.integration._e2b_log_capture import stream_death_events_since
from tests.unit.factories import make_upstream_definition

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK idle-pause test skipped",
)

CALL_SPACING_SECONDS = 20
STEADY_PHASE_SECONDS = 140
LONG_CALL_SECONDS = 45
# How long past the window E2B may take to act on a deadline.
PAUSE_SLACK_SECONDS = 15

# A minimal stdio MCP server with two tools: ``ping`` answers at once,
# ``sleep`` answers after the requested number of seconds. Inline via
# ``node -e`` like E2B-T5, so nothing has to be downloaded and the test
# controls exactly when traffic happens.
_IDLE_PROBE_MCP_JS = r"""
function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

function handle(msg) {
  if (msg.method === 'initialize') {
    send({ jsonrpc: '2.0', id: msg.id, result: {
      protocolVersion: msg.params.protocolVersion,
      capabilities: { tools: {} },
      serverInfo: { name: 'idle-probe', version: '1.0.0' },
    }});
  } else if (msg.method === 'ping') {
    send({ jsonrpc: '2.0', id: msg.id, result: {} });
  } else if (msg.method === 'tools/list') {
    send({ jsonrpc: '2.0', id: msg.id, result: { tools: [
      { name: 'ping', description: 'Answer at once',
        inputSchema: { type: 'object', properties: {} } },
      { name: 'sleep', description: 'Answer after N seconds',
        inputSchema: { type: 'object',
          properties: { seconds: { type: 'number' } },
          required: ['seconds'] } },
      { name: 'chatter', description: 'Start talking with nobody asking',
        inputSchema: { type: 'object', properties: {} } },
    ]}});
  } else if (msg.method === 'tools/call') {
    const args = msg.params.arguments || {};
    const ms = msg.params.name === 'sleep'
      ? Math.round(1000 * Number(args.seconds)) : 0;
    if (msg.params.name === 'chatter') {
      // Every 7 s: an answer to a request nobody sent, and a notice
      // that would make a gateway re-list everything.
      setInterval(() => {
        send({ jsonrpc: '2.0', id: 424242, result: {} });
        send({ jsonrpc: '2.0', method: 'notifications/tools/list_changed' });
      }, 7000);
    }
    // The id goes back as text, as some real servers do.
    setTimeout(() => send({ jsonrpc: '2.0', id: String(msg.id), result: {
      content: [{ type: 'text', text: 'ok after ' + ms + ' ms' }],
      isError: false,
    }}), ms);
  } else if (msg.id !== undefined && msg.id !== null) {
    send({ jsonrpc: '2.0', id: msg.id, error:
      { code: -32601, message: 'method not found' } });
  }
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
    let msg;
    try { msg = JSON.parse(line); } catch (e) { continue; }
    handle(msg);
  }
});
"""


def make_idle_probe_upstream() -> UpstreamDefinition:
    upstream = make_upstream_definition(
        id=f"e2e-idle-{TEST_RUN_ID}", command="node",
    )
    upstream.stdio.args = ["-e", _IDLE_PROBE_MCP_JS]  # type: ignore[union-attr]
    upstream.stdio.env = {}  # type: ignore[union-attr]
    return upstream


def text_of(result: mcp_types.CallToolResult) -> str:
    assert not result.isError, f"the call failed: {result}"
    block = result.content[0]
    assert isinstance(block, mcp_types.TextContent)
    return block.text


@pytest.mark.timeout(480)
@pytest.mark.asyncio
async def test_a_sandbox_in_use_never_pauses_and_an_idle_one_does() -> None:
    """The production bug, end to end, against the real E2B API.

    Fails without the pause timer: the first phase pauses 30 s after
    the session opens, in the middle of the calls.
    """
    instance = f"e2e-idle-{TEST_RUN_ID}"
    service = make_service(instance=instance)
    upstream = make_idle_probe_upstream()
    errlog = LogBuffer()
    client = make_test_client()
    try:
        async with service.session(
            session_id=f"{instance}-s",
            org_id=f"acme-idle-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_resources(1.0, 1024),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as sandbox_session:
            assert sandbox_session.transport_failed is not None
            transport_failed = sandbox_session.transport_failed
            async with ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            ) as client_session:
                await asyncio.wait_for(
                    client_session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                in_use_since = time.monotonic_ns()

                # Phase 1: steady use, a call every 20 s.
                started = time.monotonic()
                calls = 0
                while time.monotonic() - started < STEADY_PHASE_SECONDS:
                    # Checked before each call, so a pause reads as a
                    # pause rather than as the closed stream it causes.
                    assert not transport_failed.is_set(), (
                        f"the sandbox paused during steady use, after "
                        f"{calls} calls {CALL_SPACING_SECONDS} s apart, "
                        f"against a {IDLE_PAUSE_SECONDS} s window"
                    )
                    result = await asyncio.wait_for(
                        client_session.call_tool("ping", {}), timeout=30,
                    )
                    assert text_of(result).startswith("ok"), result
                    calls += 1
                    await asyncio.sleep(CALL_SPACING_SECONDS)
                assert not stream_death_events_since(in_use_since), (
                    f"the sandbox paused during steady use ({calls} calls, "
                    f"one every {CALL_SPACING_SECONDS} s, against a "
                    f"{IDLE_PAUSE_SECONDS} s window)"
                )
                assert not transport_failed.is_set()

                # Phase 2: one call longer than the window, nothing else.
                result = await asyncio.wait_for(
                    client_session.call_tool(
                        "sleep", {"seconds": LONG_CALL_SECONDS},
                    ),
                    timeout=LONG_CALL_SECONDS + 30,
                )
                last_answer = time.monotonic()
                assert text_of(result).startswith("ok"), result
                assert not stream_death_events_since(in_use_since), (
                    f"a {LONG_CALL_SECONDS} s call was paused underneath, "
                    f"against a {IDLE_PAUSE_SECONDS} s window"
                )
                assert not transport_failed.is_set()

                # Phase 3: silence from the caller, while the server
                # talks on its own. The sandbox must pause on schedule.
                result = await asyncio.wait_for(
                    client_session.call_tool("chatter", {}), timeout=30,
                )
                last_answer = time.monotonic()
                assert text_of(result).startswith("ok"), result
                quiet_since = time.monotonic_ns()
                give_up = (
                    last_answer + IDLE_PAUSE_SECONDS
                    + MIN_REFRESH_GAP_SECONDS + PAUSE_SLACK_SECONDS
                )
                while (
                    not transport_failed.is_set()
                    and time.monotonic() < give_up
                ):
                    await asyncio.sleep(0.5)
                paused_after = time.monotonic() - last_answer
                assert transport_failed.is_set(), (
                    f"an idle sandbox must still pause: nothing after "
                    f"{paused_after:.0f} s against a "
                    f"{IDLE_PAUSE_SECONDS} s window"
                )
                assert stream_death_events_since(quiet_since)
                assert paused_after >= IDLE_PAUSE_SECONDS - 1, (
                    f"paused {paused_after:.1f} s after the last answer, "
                    f"sooner than the {IDLE_PAUSE_SECONDS} s window"
                )
                # The stream dies as the pause starts; the snapshot can
                # take a few seconds more to show in E2B's own listing.
                states: list[str] = []
                for _ in range(15):
                    infos = await client.list_sandboxes(
                        metadata_filter={"mcpolis_instance": instance},
                    )
                    states = [info.state for info in infos]
                    if states == ["paused"]:
                        break
                    await asyncio.sleep(1)
                assert states == ["paused"], (
                    f"E2B must report the sandbox paused; got {states}"
                )
                print(
                    f"\n[idle-pause] {calls} steady calls, one "
                    f"{LONG_CALL_SECONDS} s call, no pause; with the "
                    f"server chattering, paused {paused_after:.1f} s "
                    f"after the last answer",
                )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis E2B templates not published on the active account — "
                "run `cd runner/e2b-templates && make build`.",
            )
        tail = errlog.get_output()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise
    finally:
        await sweep_kill(client, instance)
