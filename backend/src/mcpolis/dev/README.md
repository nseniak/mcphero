# `mcpolis.dev` — bundled demo MCP server

A "kitchen sink" demo upstream that exercises every surface MCP Hero
forwards: tools, resources, resource templates, prompts, and the MCP
Apps widget extension (five widget kinds: inline, fullscreen, pip,
counter, solar-system).

Used by:

* the e2e test harness — `tests/e2e/test_mcp_server.py` is a thin shim that
  imports `main()` from this package and runs it on `127.0.0.1:9999`;
* the manual smoke loop, `bash start.sh cloud --with-demo` mounts
  the demo at `/dev/mcp-demo` on the same backend, auto-registers it
  as a service-account upstream, and exposes it through the existing
  dev tunnel (if you run one) without a second connector.

## Manual smoke

```bash
bash start.sh cloud --with-demo
```

Then:

1. **MCP Inspector** at `https://dev.example.com/mcp` (or
   `http://localhost:8080/mcp`). Tools list shows the eight demo tools
   plus three solar-system callbacks; prompts list shows two; resources
   list shows the static `test://hello-world` plus five widget shells.
2. **Claude Desktop**: add `https://dev.example.com/mcp` as a
   custom connector.
   * "open the inline widget" → widget renders inline; click button →
     follow-up prompt arrives in chat.
   * "open the counter widget" → ticks visible (backend pushes one
     per second over a WebSocket).
   * "open the solar-system widget" → click a planet, then ask
     "explain it" in chat → explanation appears in the tooltip.

## What the auto-registration does

The demo has two independent toggles:

- `MCPOLIS_DEMO_MOUNT=1` mounts the demo at `/dev/mcp-demo` so any
  client (local browser, prod smoke tests) can hit it as an HTTP
  upstream target.
- `MCPOLIS_DEMO_SEED=1` auto-registers it as a service-account
  upstream (`id="mcp-demo"`, `display_name="MCP Hero demo"`,
  `url="<server_url>/dev/mcp-demo/mcp"`) on the default org
  (standalone) or on the first org found (cloud). Idempotent —
  re-runs on every boot but skips when the row already exists.

`bash start.sh` flips both on; prod sets only `MCPOLIS_DEMO_MOUNT=1`
so the smoke-test endpoint exists without dropping a junk upstream
on the first real customer org.

## Editing widget JS

Widget JS files live under `widgets/` and are served with
`Cache-Control: no-store` from `/dev/mcp-demo/widget/<name>.js`. The
MCP resource body is a 4-line shell that dynamically imports each JS
file with a `?t=<now>` cache-buster — edits don't require removing
the connector, just open a new chat. See FINDINGS §12 in the POC for
why this pattern is recommended.

## Disabling

Unset `MCPOLIS_DEMO_MOUNT` and `MCPOLIS_DEMO_SEED` (the defaults), or
pass `--no-demo` to `start.sh`. The mount is silently skipped and
the demo's auto-registration code path doesn't run.
