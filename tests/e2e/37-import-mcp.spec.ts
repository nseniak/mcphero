/**
 * Bulk MCP import end-to-end (the .claude.json path).
 *
 * Exercises what the backend/unit and frontend-helper tests can't: the
 * real drop-file -> grouped preview -> inline id edit -> confirm ->
 * upstreams-created round trip through a live backend + frontend.
 *
 *   1. A .claude.json (top-level user scope + per-project mcpServers)
 *      previews as grouped, project-titled tables; project ids are
 *      project-PREFIXED (web-github), the user-scope id stays bare, and a
 *      byte-identical cross-project server is tagged as a duplicate.
 *   2. Editing an id via the pencil and confirming creates exactly the
 *      selected upstreams under the chosen ids; the deselected duplicate
 *      is NOT created.
 *   3. Two selected rows sharing an id surface an inline error and
 *      disable Confirm.
 *
 * Runs against the seeded ``acme-corp`` (Team plan -> no upstream caps).
 * Ids are prefixed ``e2eimp-`` and cleaned up before/after each test so
 * the spec is robust on a warm (non ``--clean``) backend.
 */
import { expect, test, type Page } from "@playwright/test";
import { TEST_MCP_URL, loginAs } from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

const SENTRY = "e2eimp-sentry";
// Project basenames are deliberately collision-unlikely with seeded
// upstreams. ``e2edup`` carries a github config byte-identical to
// ``e2eweb``'s, so it previews as a duplicate (and defaults deselected).
const CLAUDE_JSON = {
  mcpServers: { [SENTRY]: { url: `${TEST_MCP_URL}/sentry` } },
  projects: {
    "/home/u/e2eweb": { mcpServers: { github: { url: `${TEST_MCP_URL}/web` } } },
    "/home/u/e2eapi": { mcpServers: { github: { url: `${TEST_MCP_URL}/api` } } },
    "/home/u/e2edup": { mcpServers: { github: { url: `${TEST_MCP_URL}/web` } } },
  },
};

// Every id the spec may create — deleted up front and after each test.
const TOUCHED = [
  SENTRY,
  "e2eweb-github",
  "e2eapi-github",
  "e2edup-github",
  "renamed-api",
];

async function cleanup(page: Page): Promise<void> {
  for (const id of TOUCHED) {
    await page.request.delete(`/api/admin/upstreams/${id}`).catch(() => {});
  }
}

/** Open the Upstreams page, launch the import dialog, drop the blob, and
 *  wait for the grouped preview to render. */
async function openImportPreview(page: Page): Promise<void> {
  await page.goto(`/orgs/${ORG}/admin/upstream`);
  await expect(
    page.getByRole("heading", { name: "Upstream MCPs" }),
  ).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Import JSON config" }).click();
  await expect(page.getByRole("heading", { name: "Import MCPs" })).toBeVisible();
  await page.setInputFiles('input[type="file"]', {
    name: "claude.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(CLAUDE_JSON)),
  });
}

test.describe.configure({ mode: "serial" });

test.describe("Bulk MCP import (.claude.json)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await cleanup(page);
  });

  test.afterEach(async ({ page }) => {
    await cleanup(page);
  });

  test("groups by project, prefixes ids, flags duplicates, imports edited ids", async ({ page }) => {
    await openImportPreview(page);

    // Project sections render with the basename title + full path.
    await expect(page.getByText("e2eweb", { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("/home/u/e2eweb")).toBeVisible();
    await expect(page.getByText("e2eapi", { exact: true })).toBeVisible();

    // Project ids are project-prefixed; the user-scope id stays bare.
    await expect(page.getByText("e2eweb-github", { exact: true })).toBeVisible();
    await expect(page.getByText("e2eapi-github", { exact: true })).toBeVisible();
    await expect(page.getByText(SENTRY, { exact: true })).toBeVisible();

    // The byte-identical cross-project server is tagged as a duplicate.
    await expect(page.getByText("duplicate of e2eweb-github")).toBeVisible();

    // Edit the api id via the pencil (only one row enters edit mode, so a
    // page-level textbox lookup is unambiguous).
    await page
      .locator("tr", { hasText: "e2eapi-github" })
      .getByRole("button", { name: "Edit ID" })
      .click();
    await page.getByRole("textbox").fill("renamed-api");

    // Confirm — 3 selected (sentry + web + the renamed api); the duplicate
    // defaulted to deselected.
    await page.getByRole("button", { name: /^Import 3 MCPs$/ }).click();

    await expect(page.getByText("3 added")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("renamed-api")).toBeVisible();
    await page.getByRole("button", { name: "Done" }).click();

    // Back on the list: the chosen ids exist as upstream rows; the
    // deselected duplicate was never created.
    await expect(page.locator('a[href$="/upstream/renamed-api"]')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('a[href$="/upstream/e2eweb-github"]')).toBeVisible();
    await expect(page.locator(`a[href$="/upstream/${SENTRY}"]`)).toBeVisible();
    await expect(page.locator('a[href$="/upstream/e2edup-github"]')).toHaveCount(0);
  });

  test("duplicate ids across selected rows disable Confirm", async ({ page }) => {
    await openImportPreview(page);
    const confirm = page.getByRole("button", { name: /^Import \d+ MCPs$/ });
    await expect(confirm).toBeEnabled();

    // Rename the api id to collide with the web id.
    await page
      .locator("tr", { hasText: "e2eapi-github" })
      .getByRole("button", { name: "Edit ID" })
      .click();
    await page.getByRole("textbox").fill("e2eweb-github");

    await expect(page.getByText("Duplicate ID in this import").first()).toBeVisible();
    await expect(confirm).toBeDisabled();
  });
});
