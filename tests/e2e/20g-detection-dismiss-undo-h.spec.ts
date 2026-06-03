/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Detection dismiss undo (§H)".
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

test.describe("Detection dismiss undo (§H)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Undo link clears the per-MCP dismiss flag and disappears", async ({
    page,
  }) => {
    const id = uniqueId("undo");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        { [id]: { url: `${TEST_MCP_URL}/mcp` } },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Pre-set the per-MCP dismiss flag and reload so the page reads it.
    await page.evaluate((upstreamId: string) => {
      localStorage.setItem(
        `mcpolis:secret-scan-dismissed:${upstreamId}`,
        "true",
      );
    }, id);
    await page.reload();
    // The Variables card lives inside the Connection card on the
    // detail page; both view and edit modes render it inline (no
    // collapsible JSON Configuration panel anymore).
    const undo = page.getByRole("button", {
      name: /Show password detection again/,
    });
    await expect(undo).toBeVisible();
    await undo.click();
    // Link disappears; localStorage key cleared.
    await expect(undo).toHaveCount(0);
    const flag = await page.evaluate(
      (upstreamId: string) =>
        localStorage.getItem(`mcpolis:secret-scan-dismissed:${upstreamId}`),
      id,
    );
    expect(flag).toBeNull();
  });
});
