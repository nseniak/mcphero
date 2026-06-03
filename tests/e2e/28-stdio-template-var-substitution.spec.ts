/**
 * End-to-end proof that ``${NAME}`` substitution + Sandbox-file
 * materialization actually wire data into a real stdio MCP's
 * spawned process — exercised through the dashboard + gateway,
 * not just the unit substitution helper.
 *
 * The fixture is an inline FastMCP shipped via
 * ``uvx --from mcp python -c "<inline>"`` so the same spec works
 * against both the E2B sandbox (the orchestrator's default when
 * ``E2B_API_KEY`` is set in the parent env) and the
 * ``local-subprocess`` fallback. ``uvx`` is in the launcher's
 * command allowlist; it fetches ``mcp`` on demand inside the
 * sandbox so the inline script can ``import mcp.server.fastmcp``.
 *
 * Three contracts:
 *
 * 1. ``${TOKEN}`` in ``stdio.env`` is resolved against the
 *    Variable repo at session start; the spawned process sees the
 *    substituted plaintext via ``os.environ`` (read back through
 *    the ``read_env`` tool over the gateway).
 *
 * 2. An uploaded Sandbox file lands at the resolved
 *    ``target_path``; the spawned process reads its contents via
 *    ``open()`` (through the ``read_file`` tool).
 *
 * 3. A missing reference (``${UNDEFINED}`` with no Variable
 *    defined) fails closed: the upstream stays disconnected.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import {
  ADMIN,
  ORG,
  loginAs,
  uniqueId,
} from "./_template_vars_helpers";
import {
  BACKEND_URL,
  apiLoginAs,
  makeMcpClient,
  mintMcpToken,
} from "./helpers";

// ``uvx --from mcp python -c "<INLINE>"`` runs the inline script
// in an ephemeral environment with the ``mcp`` Python package
// installed. The script exposes two tools the e2e specs rely on:
// ``read_env(name)`` and ``read_file(path)``.
const INLINE_MCP_SCRIPT =
  "import os\n"
  + "from mcp.server.fastmcp import FastMCP\n"
  + "s = FastMCP('e2e-inline-mcp')\n"
  + "@s.tool()\n"
  + "def read_env(name: str) -> str:\n"
  + "    return os.environ.get(name, '')\n"
  + "@s.tool()\n"
  + "def read_file(path: str) -> str:\n"
  + "    return open(path).read()\n"
  + "s.run(transport='stdio')\n";

// Cold-pull of the python E2B template + ``uvx install mcp`` runs
// well under 60s in steady state but can spike on first-of-day
// pulls; give the connection plenty of headroom.
const READY_TIMEOUT_MS = 90_000;

async function adminApi(request: APIRequestContext): Promise<APIRequestContext> {
  await apiLoginAs(request, ADMIN);
  return request;
}

async function pollReady(
  api: APIRequestContext,
  upstreamId: string,
  timeoutMs = READY_TIMEOUT_MS,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const r = await api.get(`${BACKEND_URL}/api/admin/upstreams/${upstreamId}`);
    if (r.status() === 200) {
      const body = await r.json();
      if (body.ready === true) return;
    }
    await new Promise((res) => setTimeout(res, 500));
  }
  const r = await api.get(`${BACKEND_URL}/api/admin/upstreams/${upstreamId}`);
  throw new Error(
    `Upstream ${upstreamId} never reached ready=true within ${timeoutMs}ms; `
      + `final status=${r.status()} body=${await r.text()}`,
  );
}

test.describe.configure({ timeout: 180_000 });

test.describe("Stdio template-var substitution + Sandbox files — runtime", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Defined ${VAR} in stdio.env arrives at the spawned process", async ({
    request,
  }) => {
    const api = await adminApi(request);
    const upstreamId = uniqueId("subst-env");
    const expected = `e2e-token-${Date.now().toString(36)}`;

    const createUpstream = await api.post(
      `${BACKEND_URL}/api/admin/upstreams`,
      {
        data: {
          id: upstreamId,
          display_name: "subst-env-stdio",
          command: "uvx",
          args: ["--from", "mcp", "python", "-c", INLINE_MCP_SCRIPT],
          env: { E2E_TEST_TOKEN: "${E2E_TEST_TOKEN}" },
          auth_mode: "service_account",
        },
      },
    );
    expect(createUpstream.status()).toBe(201);

    const setVar = await api.put(
      `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/template-vars/E2E_TEST_TOKEN`,
      { data: { value: expected, is_secret: false } },
    );
    expect(setVar.status()).toBe(200);

    const reconnect = await api.post(
      `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/reconnect`,
    );
    expect(reconnect.status()).toBe(200);
    await pollReady(api, upstreamId);

    const token = await mintMcpToken(request, ADMIN, ORG);
    let client: Client | null = null;
    try {
      client = await makeMcpClient(token, ORG, "mcp");
      const resp = await client.callTool({
        name: `${ORG}__${upstreamId}__read_env`,
        arguments: { name: "E2E_TEST_TOKEN" },
      });
      const text = JSON.stringify(resp.content);
      expect(text).toContain(expected);
    } finally {
      if (client) await client.close().catch(() => {});
      await api
        .delete(`${BACKEND_URL}/api/admin/upstreams/${upstreamId}`)
        .catch(() => undefined);
    }
  });

  test("Uploaded Sandbox file lands at target_path; MCP reads its contents", async ({
    request,
  }) => {
    const api = await adminApi(request);
    const upstreamId = uniqueId("subst-file");
    const expected = `e2e-file-body-${Date.now().toString(36)}`;
    // ``${HOME}`` resolves to ``/home/user`` on every published
    // mcpolis E2B template (and is a sane default for the local-
    // subprocess fallback when the operator runs as themselves).
    const targetPath = "${HOME}/.config/e2e-cred.txt";
    const expectedResolvedPath = "/home/user/.config/e2e-cred.txt";

    const createUpstream = await api.post(
      `${BACKEND_URL}/api/admin/upstreams`,
      {
        data: {
          id: upstreamId,
          display_name: "subst-file-stdio",
          command: "uvx",
          args: ["--from", "mcp", "python", "-c", INLINE_MCP_SCRIPT],
          auth_mode: "service_account",
        },
      },
    );
    expect(createUpstream.status()).toBe(201);

    const uploadFile = await api.put(
      `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/sandbox-files/cred`,
      {
        data: {
          contents: expected,
          target_path: targetPath,
          display_name: "Test credential",
        },
      },
    );
    expect(uploadFile.status()).toBe(200);

    const reconnect = await api.post(
      `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/reconnect`,
    );
    expect(reconnect.status()).toBe(200);
    await pollReady(api, upstreamId);

    const token = await mintMcpToken(request, ADMIN, ORG);
    let client: Client | null = null;
    try {
      client = await makeMcpClient(token, ORG, "mcp");
      const resp = await client.callTool({
        name: `${ORG}__${upstreamId}__read_file`,
        arguments: { path: expectedResolvedPath },
      });
      const text = JSON.stringify(resp.content);
      expect(text).toContain(expected);
    } finally {
      if (client) await client.close().catch(() => {});
      await api
        .delete(`${BACKEND_URL}/api/admin/upstreams/${upstreamId}`)
        .catch(() => undefined);
    }
  });

  test("Missing ${VAR} keeps the upstream disconnected (fail-closed)", async ({
    request,
  }) => {
    const api = await adminApi(request);
    const upstreamId = uniqueId("subst-missing");

    // No Variable defined for the referenced ${UNDEFINED_E2E_VAR}.
    const createUpstream = await api.post(
      `${BACKEND_URL}/api/admin/upstreams`,
      {
        data: {
          id: upstreamId,
          display_name: "subst-missing-stdio",
          command: "uvx",
          args: ["--from", "mcp", "python", "-c", INLINE_MCP_SCRIPT],
          env: { LEAK: "${UNDEFINED_E2E_VAR}" },
          auth_mode: "service_account",
        },
      },
    );
    expect(createUpstream.status()).toBe(201);

    const reconnect = await api.post(
      `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/reconnect`,
    );
    expect(reconnect.status()).toBe(200);

    // Fail-closed is fast — the resolver raises before any sandbox
    // is created. Poll a short window.
    let finalReady: boolean | null = null;
    let finalReason: string | null = null;
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline) {
      const r = await api.get(
        `${BACKEND_URL}/api/admin/upstreams/${upstreamId}`,
      );
      if (r.status() === 200) {
        const body = await r.json();
        if (body.ready === false && body.disconnect_reason) {
          finalReady = false;
          finalReason = body.disconnect_reason ?? null;
          break;
        }
      }
      await new Promise((res) => setTimeout(res, 200));
    }

    try {
      expect(finalReady).toBe(false);
      expect((finalReason ?? "").toLowerCase()).toContain(
        "undefined_e2e_var",
      );
    } finally {
      await api
        .delete(`${BACKEND_URL}/api/admin/upstreams/${upstreamId}`)
        .catch(() => undefined);
    }
  });
});
