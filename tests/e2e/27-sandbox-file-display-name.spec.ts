/**
 * Sandbox files use a two-field name model:
 *
 *   - ``name`` is the URL-safe storage key (path component on
 *     PUT / DELETE).
 *   - ``display_name`` is the free-form human label shown in the
 *     dashboard listing and in the Replace modal.
 *
 * The dashboard surfaces only the display name; the storage id is
 * an opaque slug the operator rarely sees. This spec locks both
 * the listing column ("Display name", showing the human label) and
 * the round-trip through the API:
 *
 *   1. Files created with ``display_name`` round-trip with that
 *      label visible in the Files table.
 *   2. Legacy / scripted callers that omit ``display_name`` get a
 *      label defaulted to the URL ``{name}`` (no empty cells).
 *   3. The relaxed name grammar accepts URL-safe slugs
 *      (``gcp-cred``, ``kubeconfig.dev``) — not just the historic
 *      UPPER_SNAKE form.
 */
import { expect, test } from "@playwright/test";

import {
  ADMIN,
  ORG,
  loginAs,
  uniqueId,
} from "./_template_vars_helpers";

test.describe("Sandbox file display_name", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Listing shows the display_name in the Display name column", async ({
    page,
  }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("display-name");
    await page.evaluate(async (upstreamId: string) => {
      await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "display-name-test",
          command: "python3",
          args: ["-c", "pass"],
          auth_mode: "service_account",
        }),
      });
      // URL-safe slug + free-form display label.
      const r = await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/gcp-cred`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "{}",
            target_path: "${HOME}/.config/gcloud/credentials.json",
            display_name: "GCP service account",
          }),
        },
      );
      if (!r.ok) {
        throw new Error(
          `set sandbox file failed: ${r.status} ${await r.text()}`,
        );
      }
    }, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Listing column header reads "Display name", not "Name".
    await expect(
      page.getByRole("columnheader", { name: /^Display name$/ }),
    ).toBeVisible({ timeout: 10_000 });
    // The row renders the display label, not the URL-safe id.
    await expect(
      page.getByText(/GCP service account/),
    ).toBeVisible();

    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });

  test("Display name defaults to the URL slug when omitted", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("legacy-display");
    const result = await page.evaluate(async (upstreamId: string) => {
      await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "legacy-display-test",
          command: "python3",
          args: ["-c", "pass"],
          auth_mode: "service_account",
        }),
      });
      const r = await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/legacy`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "x",
            target_path: "${HOME}/legacy.txt",
            // No display_name in the body.
          }),
        },
      );
      const body = (await r.json()) as { name: string; display_name: string };
      return { status: r.status, name: body.name, displayName: body.display_name };
    }, id);
    expect(result.status).toBe(200);
    expect(result.name).toBe("legacy");
    expect(result.displayName).toBe("legacy");

    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });

  test("Backend accepts URL-safe slug names (not just UPPER_SNAKE)", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("slug-name");
    const statuses = await page.evaluate(async (upstreamId: string) => {
      await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "slug-name-test",
          command: "python3",
          args: ["-c", "pass"],
          auth_mode: "service_account",
        }),
      });
      const out: Record<string, number> = {};
      for (const slug of ["gcp-cred", "kubeconfig.dev", "AWS_PROFILE"]) {
        const r = await fetch(
          `/api/admin/upstreams/${upstreamId}/sandbox-files/${slug}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              contents: "x",
              target_path: "${HOME}/x",
            }),
          },
        );
        out[slug] = r.status;
      }
      // Disallowed: spaces (URL-encoded as %20).
      const bad = await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/has%20space`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "x",
            target_path: "${HOME}/x",
          }),
        },
      );
      out["has space"] = bad.status;
      return out;
    }, id);
    expect(statuses["gcp-cred"]).toBe(200);
    expect(statuses["kubeconfig.dev"]).toBe(200);
    expect(statuses["AWS_PROFILE"]).toBe(200);
    expect(statuses["has space"]).toBe(400);

    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });
});
