/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Detection dialog — existing-name awareness".
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

test.describe("Detection dialog — existing-name awareness", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Replace existing label shows when buffered env var matches finding's default", async ({
    page,
  }) => {
    const id = uniqueId("dup-detect");
    await openAddForm(page);
    // Step 1: seed a buffered GITHUB_TOKEN before any detection runs.
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          { [id]: { url: `${TEST_MCP_URL}/mcp` } },
          null,
          2,
        ),
      );
    await page.getByRole("button", { name: /Add variable/ }).click();
    const modal = page
      .getByRole("dialog")
      .filter({ hasText: /Add variable/ });
    await modal.getByLabel(/Name/i).fill("GITHUB_TOKEN");
    await modal
      .getByPlaceholder(/Paste value/)
      .fill("seeded-existing-1234567890");
    await modal.getByRole("button", { name: /^Save$/ }).click();
    // Step 2: change JSON to env.GITHUB_PERSONAL_ACCESS_TOKEN with a
    // ghp_ token. PATTERN_NAME_HINTS.github_token suggests
    // GITHUB_TOKEN as the default — that matches the buffered var,
    // so the dialog should surface the replacement intent.
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        {
          [id]: {
            command: "npx",
            args: ["-y", "anything"],
            env: { GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_examplexxxxxxxxxxxxxxxxxxxxxxxxx" },
          },
        },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    const dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Possible passwords in JSON/ });
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: /Replace existing GITHUB_TOKEN/ }),
    ).toBeVisible();
    // The "Move to variables" label is gone for this row — the dialog
    // surfaces only the replace intent.
    await expect(
      dialog.getByRole("button", { name: /^Move to variables$/ }),
    ).toHaveCount(0);
  });
});
