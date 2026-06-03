/**
 * Pin the bare-slug (no ``.md``) variant of /docs/stdio-authent
 * so DocsPage's slug-strip stays bidirectional.
 */
import { test, expect } from "@playwright/test";

import { loginAs, ORG, ADMIN } from "./_template_vars_helpers";

test.describe("docs/stdio-authent.md anchor contract", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Bare /docs/stdio-authent (no extension) also resolves", async ({ page }) => {
    await page.goto("/docs/stdio-authent");
    await expect(
      page.getByRole("heading", { name: /^Stdio MCP authentication/ }),
    ).toBeVisible();
  });
});
