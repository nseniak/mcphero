import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

/**
 * Tool-refresh button on the upstream detail page (admin).
 *
 * Exercises the ``POST /upstreams/{id}/refresh-tools`` endpoint and its
 * UI affordance against the pre-seeded, active ``test-tools`` upstream
 * (a service_account HTTP MCP). The endpoint reattaches the live
 * session via the same ``acquire_upstream_session`` helper a tool call
 * uses, so this guards the refresh path; ``10-mcp-tool-invocation``
 * guards that tool calls still work through the shared helper.
 */
test.describe("Upstream tool refresh", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("refresh endpoint re-pulls tools on an active upstream", async ({
    page,
  }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream/test-tools`);
    await expect(page.getByText("echo", { exact: true })).toBeVisible({
      timeout: 15_000,
    });

    // Direct endpoint check: an active upstream refreshes and stays
    // connected (connected=true, no error).
    const result = await page.evaluate(async () => {
      const r = await fetch("/api/admin/upstreams/test-tools/refresh-tools", {
        method: "POST",
      });
      return { status: r.status, body: await r.json() };
    });
    expect(result.status).toBe(200);
    expect(result.body.connected).toBe(true);
    expect(result.body.error ?? null).toBeNull();
  });

  test("Refresh button refreshes without an error popup; tools persist", async ({
    page,
  }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream/test-tools`);
    await expect(page.getByText("echo", { exact: true })).toBeVisible({
      timeout: 15_000,
    });

    const refresh = page.getByRole("button", { name: /^Refresh$/ });
    await expect(refresh).toBeEnabled();
    await refresh.click();

    // No failure popup should appear (the success path).
    await expect(
      page.getByText("Couldn't refresh tools"),
    ).toHaveCount(0);

    // The min-spin floor keeps the button busy briefly, then it
    // re-enables and the tool list is intact.
    await expect(refresh).toBeEnabled({ timeout: 15_000 });
    await expect(page.getByText("echo", { exact: true })).toBeVisible();
    await expect(page.getByText("add", { exact: true })).toBeVisible();
    await expect(page.getByText("greet", { exact: true })).toBeVisible();
  });
});
