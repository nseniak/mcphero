/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Per-MCP secrets — create wizard (buffered mode)".
 *
 * Each describe lives in its own spec file so the
 * orchestrator (tests/run-e2e-tests.py) can spread them
 * across shards. Shared helpers in
 * ``_template_vars_helpers.ts``.
 */
import { test, expect } from "@playwright/test";

import {
  loginAs,
  TEST_MCP_URL,
  ORG,
  ADMIN,
  openAddForm,
} from "./_template_vars_helpers";

test.describe("Per-MCP secrets — create wizard (buffered mode)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Empty SecretsManager renders alongside JSON box", async ({ page }) => {
    await openAddForm(page);
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          { "demo-empty": { url: `${TEST_MCP_URL}/mcp` } },
          null,
          2,
        ),
      );
    // The Variables block shows its title as an inline label paired
    // with the "+ Add variable" button on the same row. The label's
    // text is "Variables" (CSS uppercase only — DOM text isn't
    // transformed, so the locator matches the source casing).
    await expect(page.getByText(/^Variables$/)).toBeVisible();
    await expect(page.getByText(/No variables defined/)).toBeVisible();
    // The "Use ${MY_VAR}" help text is rendered.
    await expect(page.getByText(/Use \$\{MY_VAR\}/)).toBeVisible();
  });

  test("Add a secret in buffered mode, see it in the list", async ({ page }) => {
    await openAddForm(page);
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          { "demo-add": { url: `${TEST_MCP_URL}/mcp` } },
          null,
          2,
        ),
      );
    await page.getByRole("button", { name: /Add variable/ }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Add variable|Replace / });
    await dialog.getByLabel(/Name/i).fill("MY_TOKEN");
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("super-secret-value-xyz1234567890");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Listed with the last-4 preview.
    await expect(page.getByText("MY_TOKEN")).toBeVisible();
    await expect(page.getByText(/••••7890/)).toBeVisible();
  });

  test("Stdio JSON surfaces system HOME row + does not flag ${HOME} as undefined", async ({
    page,
  }) => {
    // Bug fix: the buffered-mode refresh used to short-circuit the
    // system-var fetch entirely, so a stdio config referencing
    // ${HOME} would land in the amber "undefined variables" callout
    // and the system row never showed. Now the wizard fires
    // ?transport=stdio and surfaces HOME the same way the detail
    // page does.
    await openAddForm(page);
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          {
            "demo-stdio-home": {
              command: "npx",
              args: ["-y", "anything"],
              env: { CRED: "${HOME}/cred.json" },
            },
          },
          null,
          2,
        ),
      );
    await expect(page.getByText("HOME").first()).toBeVisible();
    await expect(page.getByText("/home/user").first()).toBeVisible();
    await expect(page.getByText(/^system$/).first()).toBeVisible();
    // ${HOME} is now resolved by the analyser, so the amber callout
    // should not list it.
    await expect(
      page.getByText(/references undefined variables/i),
    ).toHaveCount(0);
  });

  test("HTTP JSON in the wizard does not show the system HOME row", async ({
    page,
  }) => {
    // Sibling check: when the operator pastes URL-shaped JSON, the
    // wizard fires ?transport=streamable_http and the backend
    // returns []. No system row, no /home/user value.
    await openAddForm(page);
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          { "demo-http-no-home": { url: `${TEST_MCP_URL}/mcp` } },
          null,
          2,
        ),
      );
    await expect(page.getByText(/No variables defined/)).toBeVisible();
    await expect(page.getByText(/^system$/)).toHaveCount(0);
    await expect(page.getByText("/home/user")).toHaveCount(0);
  });

  test("Reference badge updates when JSON references a buffered secret", async ({
    page,
  }) => {
    await openAddForm(page);
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          {
            "demo-ref": {
              command: "npx",
              args: ["-y", "anything"],
              env: { GITHUB_TOKEN: "${MY_TOKEN}" },
            },
          },
          null,
          2,
        ),
      );
    // Referencing a not-yet-defined variable renders the unresolved
    // warning callout with an inline Add button next to the token.
    // Scope the locator to the callout so ``${MY_TOKEN}`` in the
    // textarea content doesn't double-match.
    const callout = page
      .getByText(/references undefined variables/i)
      .locator("xpath=ancestor::div[2]");
    await expect(callout).toBeVisible();
    await expect(callout.getByText("${MY_TOKEN}")).toBeVisible();
    await callout.getByRole("button", { name: /^Add$/ }).click();
    const dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Add variable|Replace / });
    // The name field is pre-filled and locked because we used the
    // inline Add (which calls openDefine), not the manager's Add
    // button (which opens with an empty unlocked field).
    await expect(dialog.getByLabel(/Name/i)).toHaveValue("MY_TOKEN");
    await expect(dialog.getByLabel(/Name/i)).toBeDisabled();
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("ghp_resolved_value_for_reference_xyz");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Callout disappears once the name is defined; "Referenced 1×"
    // badge appears on the new row.
    await expect(
      page.getByText(/references undefined variables/i),
    ).toHaveCount(0);
    await expect(page.getByText(/Referenced 1×/)).toBeVisible();
  });

});
