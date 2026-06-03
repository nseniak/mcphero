/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Password reveal + rename (1Password-style UX)".
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

test.describe("Password reveal + rename (1Password-style UX)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Password row obfuscates by default; eye toggle reveals", async ({
    page,
  }) => {
    const id = uniqueId("reveal");
    const SECRET = "ghp_revealtestlongvalue1234abcd";
    // Seed via API (HTTP upstream so no sandbox spawn — we only care
    // about the variables UI here).
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(
      page.getByRole("heading", { name: "Upstream MCPs" }),
    ).toBeVisible({ timeout: 10_000 });
    await page.evaluate(
      async ({ upstreamId, secret, testMcpUrl }) => {
        // ``testMcpUrl`` is plumbed through from the test harness so
        // the browser context doesn't depend on the helpers import
        // (which doesn't exist in the page sandbox).
        const r = await fetch("/api/admin/upstreams", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: upstreamId,
            display_name: "Reveal Test",
            url: `${testMcpUrl}/mcp`,
            auth_mode: "service_account",
          }),
        });
        if (!r.ok) {
          throw new Error(`add upstream failed: ${r.status} ${await r.text()}`);
        }
        const r2 = await fetch(
          `/api/admin/upstreams/${upstreamId}/template-vars/SECRET_VAR`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: secret, is_secret: true }),
          },
        );
        if (!r2.ok) {
          throw new Error(`set var failed: ${r2.status} ${await r2.text()}`);
        }
      },
      { upstreamId: id, secret: SECRET, testMcpUrl: TEST_MCP_URL },
    );
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Variables card sits inline inside the Connection card on the
    // detail page — no collapsible to expand.
    // Default: obfuscated last-4 preview, no plaintext on screen.
    await expect(page.getByText("••••abcd")).toBeVisible();
    await expect(page.getByText(SECRET)).toHaveCount(0);
    // Click the eye to reveal.
    await page.getByLabel(/Reveal value/i).click();
    await expect(page.getByText(SECRET)).toBeVisible();
    // Click again to hide.
    await page.getByLabel(/Hide value/i).click();
    await expect(page.getByText(SECRET)).toHaveCount(0);
    await expect(page.getByText("••••abcd")).toBeVisible();
  });

  test("Edit modal renames a variable via SETTINGS Save", async ({ page }) => {
    const id = uniqueId("rename");
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(
      page.getByRole("heading", { name: "Upstream MCPs" }),
    ).toBeVisible({ timeout: 10_000 });
    // Seed: HTTP upstream + one password variable.
    await page.evaluate(
      async ({ upstreamId, testMcpUrl }) => {
        const r = await fetch("/api/admin/upstreams", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: upstreamId,
            display_name: "Rename Test",
            url: `${testMcpUrl}/mcp`,
            auth_mode: "service_account",
          }),
        });
        if (!r.ok) throw new Error(`add upstream failed: ${r.status}`);
        const r2 = await fetch(
          `/api/admin/upstreams/${upstreamId}/template-vars/OLD_NAME`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              value: "rename-value-1234567890",
              is_secret: true,
            }),
          },
        );
        if (!r2.ok) throw new Error(`set var failed: ${r2.status}`);
      },
      { upstreamId: id, testMcpUrl: TEST_MCP_URL },
    );
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Enter Edit mode on the SETTINGS card.
    await page.getByRole("button", { name: /^Edit$/ }).click();
    // The Edit modal pencil sits next to OLD_NAME.
    await page.getByTitle(/Replace value/).click();
    const dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Edit OLD_NAME|Add variable/ });
    const name = dialog.getByLabel(/Name/i);
    await expect(name).toHaveValue("OLD_NAME");
    await name.fill("NEW_NAME");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Modal closes; the deferred buffer now holds delete-OLD +
    // set-NEW. Optimistic display reflects this.
    await expect(page.getByText("NEW_NAME")).toBeVisible();
    // SETTINGS Save flushes the buffer to the backend.
    await page.getByRole("button", { name: /^Save$/ }).click();
    // After flush, the server lists NEW_NAME and not OLD_NAME.
    await expect
      .poll(async () => {
        return page.evaluate(async (upstreamId: string) => {
          const r = await fetch(
            `/api/admin/upstreams/${upstreamId}/template-vars`,
          );
          const list = (await r.json()) as { name: string }[];
          return list.map((s) => s.name).sort();
        }, id);
      }, { timeout: 10_000 })
      .toEqual(["NEW_NAME"]);
  });
});
