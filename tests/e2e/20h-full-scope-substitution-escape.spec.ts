/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Full-scope substitution + escape".
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

test.describe("Full-scope substitution + escape", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Unresolved-callout fires for ${MISSING} reference inside args", async ({
    page,
  }) => {
    // Substitution covers every user-controlled string — args /
    // command / url too. The amber callout must fire on a
    // ``${MISSING}`` token anywhere the runtime would substitute.
    const id = uniqueId("args-unresolved");
    await openAddForm(page);
    const json = JSON.stringify(
      {
        [id]: {
          command: "python3",
          args: ["-c", "print(${MISSING_FOO});"],
        },
      },
      null,
      2,
    );
    await page.locator("textarea").first().fill(json);
    // Scope the callout — ``${MISSING_FOO}`` also appears in the
    // textarea content, so a top-level getByText hits two elements.
    const callout = page
      .getByText(/references undefined variables/i)
      .locator("xpath=ancestor::div[2]");
    await expect(callout).toBeVisible();
    await expect(callout.getByText("${MISSING_FOO}")).toBeVisible();
    // Inline Add button next to the unresolved row opens the modal
    // pre-filled and locked on the name.
    await callout.getByRole("button", { name: /^Add$/ }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Add variable/ });
    const name = dialog.getByLabel(/Name/i);
    await expect(name).toHaveValue("MISSING_FOO");
    await expect(name).toBeDisabled();
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("resolved-long-enough-value");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Callout disappears once the name is defined in the buffer.
    await expect(
      page.getByText(/references undefined variables/i),
    ).toHaveCount(0);
  });

  test("Unresolved-callout fires in deferred mode (edit detail page)", async ({
    page,
  }) => {
    const id = uniqueId("deferred-unresolved");
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
    // Rewrite the JSON to reference a variable that doesn't exist
    // yet. The deferred TemplateVarsManager should surface the
    // unresolved callout with an Add button.
    const editor = page.locator("textarea").first();
    await editor.fill(
      JSON.stringify(
        {
          url: `${TEST_MCP_URL}/mcp`,
          headers: { "X-Token": "${EDIT_MISSING}" },
        },
        null,
        2,
      ),
    );
    const callout = page
      .getByText(/references undefined variables/i)
      .locator("xpath=ancestor::div[2]");
    await expect(callout).toBeVisible();
    await expect(callout.getByText("${EDIT_MISSING}")).toBeVisible();
    await callout.getByRole("button", { name: /^Add$/ }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Add variable/ });
    await expect(dialog.getByLabel(/Name/i)).toHaveValue("EDIT_MISSING");
    await expect(dialog.getByLabel(/Name/i)).toBeDisabled();
  });

  test("Backslash escape \\${LITERAL} is not flagged in the callout", async ({
    page,
  }) => {
    const id = uniqueId("escape");
    await openAddForm(page);
    // Mix one real reference (REAL) with one escaped literal that a
    // downstream tool's own syntax needs verbatim. Only REAL should
    // appear in the unresolved callout — \${LITERAL} round-trips
    // through the substitution layer untouched and is invisible to
    // the reference walker.
    const json = JSON.stringify(
      {
        [id]: {
          command: "python3",
          args: ["-c", "x=${REAL}; y='\\${LITERAL}'"],
        },
      },
      null,
      2,
    );
    await page.locator("textarea").first().fill(json);
    const callout = page
      .getByText(/references undefined variables/i)
      .locator("xpath=ancestor::div[2]");
    await expect(callout).toBeVisible();
    await expect(callout.getByText("${REAL}")).toBeVisible();
    await expect(callout.getByText("${LITERAL}")).toHaveCount(0);
  });

  test("Cancel in edit mode discards a ${VAR} edit to the JSON", async ({
    page,
  }) => {
    const id = uniqueId("cancel-url");
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
    // Edit the URL to add a ${VAR} reference; click Cancel.
    const editor = page.locator("textarea").first();
    const dirtyJson = JSON.stringify(
      // Preserve the literal ``${PROJECT}`` token — it's a mcpolis
      // template-variable reference, not a JS interpolation.
      { url: TEST_MCP_URL + "/${PROJECT}/mcp" },
      null,
      2,
    );
    await editor.fill(dirtyJson);
    await page.getByRole("button", { name: /^Cancel$/ }).click();
    // The saved URL on the server is unchanged. UpstreamDetail
    // exposes the resolved url at the top level.
    const detail = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}`);
      return r.json();
    }, id);
    expect(detail.url).toBe(`${TEST_MCP_URL}/mcp`);
    expect(detail.url).not.toContain("${PROJECT}");
  });
});
