/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Server-logs redaction".
 *
 * Each describe lives in its own spec file so the
 * orchestrator (tests/run-e2e-tests.py) can spread them
 * across shards. Shared helpers in
 * ``_template_vars_helpers.ts``.
 */
import { test, expect, type Page } from "@playwright/test";

import {
  loginAs,
  TEST_MCP_URL,
  ORG,
  ADMIN,
  uniqueId,
  openAddForm,
  fillJsonAndAdvance,
} from "./_template_vars_helpers";

test.describe("Server-logs redaction", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("is_secret value substituted into args is masked in Server logs", async ({
    page,
  }) => {
    // The contract: when an MCP echoes a substituted secret to its
    // stdout/stderr, the per-upstream LogBuffer (RedactingLogBuffer)
    // masks the value as ``[REDACTED:NAME]`` before storage.
    //
    // The ``python3 -c "print('Secret', '${LEAK_TEST}');"`` snippet
    // writes a non-JSON-RPC line to stdout. The
    // LocalSubprocessSandboxService's stdout pump fails to parse it
    // as JSON-RPC, then tees the line into the operator's errlog
    // (which IS the RedactingLogBuffer). The redactor masks the
    // substituted secret before the buffer stores the chunk.
    const id = uniqueId("redact");
    const SECRET_VALUE = "ghp_redactiontestvalue1234567";

    // Need a real origin before page.evaluate fetches can use a
    // relative path. Land on the upstream list page first.
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(
      page.getByRole("heading", { name: "Upstream MCPs" }),
    ).toBeVisible({ timeout: 10_000 });

    // Seed via the API: stdio MCP whose ``args`` reference the
    // password-flagged variable. ``service_account`` auto-connects on
    // create, so the spawn happens without an extra Start click.
    await page.evaluate(
      async ({ upstreamId, secretValue }) => {
        const r = await fetch("/api/admin/upstreams", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: upstreamId,
            display_name: "Redact Test",
            command: "python3",
            args: ["-c", "print('Secret', '${LEAK_TEST}');"],
            auth_mode: "service_account",
            cpu_vcpus: 1.0,
            memory_mb: 2048,
            disk_gb: 0,
            template_vars: {
              LEAK_TEST: { value: secretValue, is_secret: true },
            },
          }),
        });
        if (!r.ok) {
          throw new Error(`add upstream failed: ${r.status} ${await r.text()}`);
        }
      },
      { upstreamId: id, secretValue: SECRET_VALUE },
    );

    // Newly-added upstreams are flagged ``disabled`` by the create
    // path so a tightened boot gate doesn't silently start them.
    // Explicitly trigger a reconnect to spawn the sandbox.
    await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(
        `/api/admin/upstreams/${upstreamId}/reconnect`,
        { method: "POST" },
      );
      if (!r.ok) {
        throw new Error(`reconnect failed: ${r.status} ${await r.text()}`);
      }
    }, id);

    // Wait for the spawn → fail → log-tee chain to land. The MCP
    // handshake fails (no JSON-RPC came back), but the stdout line
    // is captured before the connection task tears down.
    await expect
      .poll(
        async () => {
          return page.evaluate(async (upstreamId: string) => {
            const r = await fetch(`/api/admin/upstreams/${upstreamId}/logs`);
            const j = await r.json();
            return (j.logs as string | null) ?? "";
          }, id);
        },
        { timeout: 30_000 },
      )
      .toContain("[REDACTED:LEAK_TEST]");

    // Pin the negative half of the contract: the plaintext must NOT
    // appear in the buffer.
    const finalLogs = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}/logs`);
      const j = await r.json();
      return (j.logs as string | null) ?? "";
    }, id);
    expect(finalLogs).not.toContain(SECRET_VALUE);
    expect(finalLogs).toContain("[REDACTED:LEAK_TEST]");
  });
});
