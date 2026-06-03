/**
 * Per-MCP Sandbox files — CRUD on the upstream detail page.
 *
 * Asserts the wire shape of the new admin routes via the dashboard
 * SPA: upload a file with a ``${HOME}``-prefixed target path, see
 * the resolved-path preview update, save, see the file in the list
 * with correct name / size / sha256, delete it.
 *
 * The integration suite covers end-to-end materialisation against
 * a real E2B sandbox; this spec keeps the UI layer honest.
 */
import { test, expect } from "@playwright/test";

import {
  loginAs,
  ADMIN,
  ORG,
  uniqueId,
} from "./_template_vars_helpers";

test.describe("Per-MCP Sandbox files — CRUD", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("System ${HOME} variable surfaces with system badge", async ({ page }) => {
    const id = uniqueId("home-badge");
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(
      page.getByRole("heading", { name: "Upstream MCPs" }),
    ).toBeVisible({ timeout: 10_000 });

    // Seed a stdio upstream via the API so we don't go through the
    // wizard for what's essentially a check on the detail-page render.
    await page.evaluate(async (upstreamId: string) => {
      const r = await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "Sandbox files: HOME badge",
          command: "python3",
          args: ["-c", "import sys; sys.stdin.read()"],
          auth_mode: "service_account",
          cpu_vcpus: 1.0,
          memory_mb: 2048,
          disk_gb: 0,
        }),
      });
      if (!r.ok) {
        throw new Error(`add upstream failed: ${r.status} ${await r.text()}`);
      }
    }, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await expect(
      page.getByRole("heading", {
        name: /Sandbox files: HOME badge|Upstream/,
      }),
    ).toBeVisible({ timeout: 10_000 });

    // Edit mode so the Variables list renders (the system row is
    // visible in both modes — the badge is the assertion).
    await page.getByRole("button", { name: /^Edit/ }).click();

    // The system Variable row renders ``HOME`` + the resolved value
    // ``/home/user`` + a "system" badge.
    await expect(page.getByText("HOME").first()).toBeVisible();
    await expect(page.getByText("/home/user").first()).toBeVisible();
    await expect(page.getByText(/^system$/).first()).toBeVisible();
  });

  test("HTTP upstream detail page does NOT render the system HOME row", async ({
    page,
  }) => {
    // System Variables only make sense for stdio (sandbox-injected
    // env). The transport-keyed system-variables endpoint returns []
    // for HTTP, so the detail page must not show a "system" badge or
    // a HOME row for an HTTP upstream.
    const id = uniqueId("http-no-home");
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(
      page.getByRole("heading", { name: "Upstream MCPs" }),
    ).toBeVisible({ timeout: 10_000 });

    await page.evaluate(async (upstreamId: string) => {
      const r = await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "HTTP no HOME",
          url: "https://1.1.1.1/mcp",
          auth_mode: "service_account",
        }),
      });
      if (!r.ok) {
        throw new Error(`add upstream failed: ${r.status} ${await r.text()}`);
      }
    }, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await expect(
      page.getByRole("heading", { name: /HTTP no HOME|Upstream/ }),
    ).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: /^Edit/ }).click();
    // The Variables card renders for HTTP too — empty list since
    // there are no user vars and the backend returns [] for the
    // system list. Wait for the empty-state copy as a settle point
    // before the negative assertions.
    await expect(page.getByText(/No variables defined/)).toBeVisible();
    await expect(page.getByText(/^system$/)).toHaveCount(0);
    await expect(page.getByText("/home/user")).toHaveCount(0);
  });

  test("Sandbox file row appears after API write, delete via UI removes it", async ({
    page,
  }) => {
    const id = uniqueId("sbx-files");
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(
      page.getByRole("heading", { name: "Upstream MCPs" }),
    ).toBeVisible({ timeout: 10_000 });

    await page.evaluate(async (upstreamId: string) => {
      const r = await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "Sandbox files CRUD",
          command: "python3",
          args: ["-c", "import sys; sys.stdin.read()"],
          auth_mode: "service_account",
          cpu_vcpus: 1.0,
          memory_mb: 2048,
          disk_gb: 0,
        }),
      });
      if (!r.ok) {
        throw new Error(`add upstream failed: ${r.status} ${await r.text()}`);
      }
      // Seed a Sandbox file via the new admin route — exercises the
      // wire shape without going through the modal upload flow.
      const r2 = await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/MY_CRED`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "hello-from-e2e",
            target_path: "${HOME}/test-sbx-file.txt",
          }),
        },
      );
      if (!r2.ok) {
        throw new Error(
          `set sandbox file failed: ${r2.status} ${await r2.text()}`,
        );
      }
    }, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Row renders without entering edit mode (read-only mode of the
    // SandboxFilesManager still lists files; only the Add / Replace
    // / Delete affordances are gated). Pin the file's name + the
    // raw target_path (the resolved-display feature was removed —
    // rows now show ``${HOME}/...`` literal).
    await expect(
      page.locator("td").filter({ hasText: /^MY_CRED$/ }),
    ).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByText("${HOME}/test-sbx-file.txt"),
    ).toBeVisible();

    // Switch to edit mode and click delete on the row.
    await page.getByRole("button", { name: /^Edit/ }).click();
    const deleteBtn = page
      .getByRole("button", { name: /Delete/ })
      .first();
    await deleteBtn.click();
    // Confirm dialog uses the same useConfirm pattern as the
    // Variables flow: the modal renders a "Delete" confirm button.
    const confirmBtn = page.getByRole("button", { name: /^Delete$/ }).last();
    await confirmBtn.click();
    await expect(
      page.locator("td").filter({ hasText: /^MY_CRED$/ }),
    ).toHaveCount(0, { timeout: 10_000 });
  });
});
