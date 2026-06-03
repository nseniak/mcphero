/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Plain (non-secret) env vars".
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

test.describe("Plain (non-secret) env vars", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Plain var renders verbatim in the list and persists across reloads", async ({
    page,
  }) => {
    const id = uniqueId("plain");
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
    await page.getByRole("button", { name: /^Edit$/ }).click();
    // Open Add modal, untick "Treat as password", save with a plain
    // value.
    await page.getByRole("button", { name: /Add variable/ }).click();
    const dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Add variable/ });
    await dialog.getByLabel(/Name/i).fill("LOG_LEVEL");
    await dialog.getByPlaceholder(/Paste value/).fill("debug");
    await dialog.getByLabel(/Treat as password/).uncheck();
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Buffered display — the value renders in clear before flush.
    await expect(page.getByText("LOG_LEVEL")).toBeVisible();
    await expect(page.getByText("debug")).toBeVisible();
    // SETTINGS Save flushes the buffer to the server.
    await page.getByRole("button", { name: /^Save$/ }).click();
    // Reload — value still visible (the API returns it for plain rows).
    await page.reload();
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await expect(page.getByText("LOG_LEVEL")).toBeVisible();
    await expect(page.getByText("debug")).toBeVisible();
  });

  test("Replace modal hides the toggle (secrecy is create-time only)", async ({
    page,
  }) => {
    const id = uniqueId("toggle");
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
    await page.getByRole("button", { name: /^Edit$/ }).click();
    // Add a secret first.
    await page.getByRole("button", { name: /Add variable/ }).click();
    let dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Add variable/ });
    await dialog.getByLabel(/Name/i).fill("ROT");
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("first-value-1234567890");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Open Replace — confirm the toggle is hidden.
    await page.getByTitle(/Replace value/).click();
    dialog = page.getByRole("dialog").filter({ hasText: /Edit ROT/ });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel(/Treat as password/)).toHaveCount(0);
  });

  test("Plain row exposes a Copy button that writes the full value to the clipboard", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const id = uniqueId("copy");
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
    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}/template-vars/COPY_TARGET`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          value: "s3://very-long-bucket-name/path/to/object/with-extra-segments",
          is_secret: false,
        }),
      });
    }, id);
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Variables card is inline inside the Connection card now — no
    // panel to expand.
    // Read-only view mode — the copy button is rendered alongside the
    // truncated value.
    const row = page.locator("li").filter({ hasText: "COPY_TARGET" });
    await row.getByLabel(/Copy value/i).click();
    // Clipboard carries the full (un-truncated) value.
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipboard).toBe(
      "s3://very-long-bucket-name/path/to/object/with-extra-segments",
    );
    // Affordance briefly flips to "Copied".
    await expect(row.getByLabel(/Copied/i)).toBeVisible();
  });
});
