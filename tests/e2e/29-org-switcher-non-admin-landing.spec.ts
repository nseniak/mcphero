import { test, expect } from "@playwright/test";
import { BACKEND_URL, createOrg, loginAs } from "./helpers";

/**
 * Regression: a user who is admin in org A and a plain ``user`` in
 * org B uses the OrgSwitcher (or the manage page's "Switch" button)
 * to flip from A to B. The switcher unconditionally navigates to
 * ``/orgs/${slug}/admin/upstream``; in B the user has no admin role,
 * so ``UpstreamsPage`` immediately fires ``GET /api/admin/upstreams``
 * which is gated by ``require_admin`` and returns
 * ``403 "Admin role required"``.
 *
 * Surfaces:
 *   - frontend/src/components/layout/OrgSwitcher.tsx (handleChange)
 *   - frontend/src/pages/OrganizationsPage.tsx (handleSwitch +
 *     post-delete fallback when remaining orgs[0] is non-admin)
 *
 * Expected (post-fix): the post-switch destination is role-aware
 * (e.g. delegate to ``/app`` and let ``DefaultRedirect`` resolve, or
 * pick ``/my-tools`` / ``/connect`` for non-admins). No admin call
 * is made, no 403 surfaces.
 */

const stamp = Date.now().toString(36);
const ADMIN = `admin-orgsw-${stamp}@test.com`;
const USER = `user-orgsw-${stamp}@test.com`;
const ORG1 = `org1-orgsw-${stamp}`;
const ORG2 = `org2-orgsw-${stamp}`;

test("OrgSwitcher must not land a non-admin user on /admin/upstream", async ({
  page,
  request,
}) => {
  // ── Admin sets up org1 (API only) ────────────────────────────────
  // ``createOrg`` does its own dev-stub login on the request context,
  // so the cookie jar carries admin's session afterwards.
  await createOrg(request, ADMIN, ORG1, "Org One");

  // The cookie's org_slug is empty after the first-login callback (no
  // orgs existed at sign-in time). Point it at org1 so the next admin
  // call resolves correctly.
  let resp = await request.post(`${BACKEND_URL}/api/orgs/${ORG1}/switch`);
  expect(resp.status()).toBe(204);

  // Pre-approve user@ as a non-admin in org1. The membership row is
  // created lazily on the user's first sign-in (login callback).
  resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: USER, role: "user" },
  });
  expect([200, 201]).toContain(resp.status());

  // ── User signs in (browser context) — materializes membership.
  await loginAs(page, USER);

  // User creates org2 via the page's request context (carries the
  // user's session cookie). Creator becomes admin of the new org.
  const pageReq = page.context().request;
  resp = await pageReq.post(`${BACKEND_URL}/api/orgs`, {
    data: { slug: ORG2, display_name: "Org Two", created_via: "manage_page" },
  });
  expect([200, 201]).toContain(resp.status());

  // Switch the cookie to org2 so the page loads with org2 as current.
  resp = await pageReq.post(`${BACKEND_URL}/api/orgs/${ORG2}/switch`);
  expect(resp.status()).toBe(204);

  // Sanity-check: as admin of org2 the admin page renders.
  await page.goto(`/orgs/${ORG2}/admin/upstream`);
  await expect(
    page.getByRole("heading", { name: /upstream/i })
  ).toBeVisible({ timeout: 10_000 });

  // ── Capture every response on the admin endpoint ─────────────────
  // We assert there is no 403 — the bug surfaces a 403 with body
  // ``Admin role required`` when the OrgSwitcher takes a non-admin to
  // ``/admin/upstream``.
  const adminResponses: Array<{ status: number; body: string }> = [];
  page.on("response", async (r) => {
    if (new URL(r.url()).pathname === "/api/admin/upstreams") {
      adminResponses.push({
        status: r.status(),
        body: await r.text().catch(() => ""),
      });
    }
  });

  // ── Trigger the bug: switch to org1 via the OrgSwitcher ──────────
  // The OrgSwitcher renders a <select> when ``user.orgs.length > 1``.
  // Scope to the select that lists ORG1 to avoid colliding with any
  // other <select> on the page.
  const switcher = page
    .locator("select")
    .filter({ has: page.locator(`option[value="${ORG1}"]`) });
  await expect(switcher).toBeVisible({ timeout: 10_000 });
  await switcher.selectOption(ORG1);

  // OrgSwitcher does ``window.location.href = /orgs/${slug}/admin/upstream``
  // unconditionally. Wait for the post-switch navigation to settle.
  await page.waitForURL(`**/orgs/${ORG1}/**`, { timeout: 10_000 });
  // Give any in-flight admin request a tick to land.
  await page.waitForTimeout(500);

  // ── Assertions (post-fix expectations) ───────────────────────────
  // 1. We must not land on /admin/* for an org where we lack admin.
  const landedPath = new URL(page.url()).pathname;
  expect(
    landedPath,
    `OrgSwitcher routed a non-admin to ${landedPath}; expected /my-tools or /connect.`
  ).not.toContain("/admin/");

  // 2. No admin endpoint should have been called at all.
  const offending = adminResponses.find((r) => r.status === 403);
  expect(
    offending,
    `Switching to a non-admin org triggered ${offending?.status} on ` +
      `/api/admin/upstreams with body "${offending?.body}". The ` +
      `OrgSwitcher must not navigate to /admin/* for non-admins.`
  ).toBeUndefined();
});
