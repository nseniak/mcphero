import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { BACKEND_URL, createOrg, loginAs } from "./helpers";

/** Switch the session to ``slug`` and re-mirror the rotated cookie.
 *
 * ``POST /api/orgs/{slug}/switch`` rotates the session cookie to carry
 * the new org slug. ``loginAs`` mirrors the backend-origin cookie onto
 * the frontend origin, but that copy was taken BEFORE the switch, so
 * without re-mirroring the SPA reads a session with no current org and
 * DefaultRedirect sends it to /signup. That made this spec flaky. */
async function switchOrgAndSyncCookie(page: Page, slug: string) {
  const context = page.context();
  const resp = await context.request.post(
    `${BACKEND_URL}/api/orgs/${slug}/switch`);
  expect(resp.status()).toBe(204);

  const cookies = await context.cookies(BACKEND_URL);
  const session = cookies.find((c) => c.name === "mcpolis_session");
  if (!session) throw new Error("switch did not leave a session cookie");
  await context.addCookies([{ ...session, domain: "localhost", path: "/" }]);
}

/**
 * Regression: the bare ``/my-tools`` path must resolve to the signed-in
 * person's own org, not fall through to the SPA catch-all.
 *
 * Two backend call sites emit this path without a slug, because neither
 * knows the viewer's slug (only the org id):
 *   - domain/services/tool_router.py — the gateway's "you are not signed
 *     in to X, open <url> and click Connect" message, returned to the AI
 *     client on a per_user_oauth session miss.
 *   - domain/services/upstream_health_check.py — the re-auth link in the
 *     connection-expired email (``/my-tools?reauth=<token>``).
 *
 * Before the fix, ``/my-tools`` matched no route, hit ``path="*"`` in
 * App.tsx and redirected to ``/`` — the marketing homepage. The user was
 * told to go somewhere that silently dropped them on a landing page.
 * nginx's ``try_files`` serves the SPA for any path, so there was no 404
 * to notice either.
 *
 * ``16-per-user-oauth.spec.ts`` asserts the gateway's *message text* but
 * never follows the URL inside it, which is why the dead link survived.
 * This spec follows it.
 */

const stamp = Date.now().toString(36);
const ADMIN = `admin-mytools-${stamp}@test.com`;
const ORG = `org-mytools-${stamp}`;

test("bare /my-tools lands on the org's My Tools page, not the homepage", async ({
  page,
  request,
}) => {
  await createOrg(request, ADMIN, ORG, "My Tools Org");

  await loginAs(page, ADMIN);
  await switchOrgAndSyncCookie(page, ORG);

  // ── The actual regression ────────────────────────────────────────
  await page.goto("/my-tools");
  await page.waitForURL(`**/orgs/${ORG}/my-tools`, { timeout: 10_000 });

  const landedPath = new URL(page.url()).pathname;
  expect(
    landedPath,
    `Bare /my-tools landed on ${landedPath}. The gateway tells users to ` +
      `open this path verbatim, so it must resolve to the viewer's own ` +
      `org, never the marketing homepage.`
  ).toBe(`/orgs/${ORG}/my-tools`);

  // The page itself must render, not just the URL match.
  await expect(
    page.getByRole("heading", { name: /my tools/i })
  ).toBeVisible({ timeout: 10_000 });
});

test("bare /my-tools preserves the ?reauth= token through the redirect", async ({
  page,
  request,
}) => {
  // The connection-expired email appends a signed ``reauth`` token.
  // The redirect must carry it to the destination: dropping the query
  // string would silently turn a one-click re-auth link into a plain
  // page visit, and the failure would be invisible (the user lands on
  // a page that looks right and simply does nothing).
  //
  // NOTE: nothing verifies this token yet — ``upstream_reauth`` is
  // signed in upstream_health_check.py and has no consumer. This
  // assertion pins the transport so the token survives the hop once
  // that consumer is built.
  const stamp2 = Date.now().toString(36) + "b";
  const admin = `admin-reauth-${stamp2}@test.com`;
  const org = `org-reauth-${stamp2}`;

  await createOrg(request, admin, org, "Reauth Org");
  await loginAs(page, admin);
  await switchOrgAndSyncCookie(page, org);

  await page.goto("/my-tools?reauth=test-token-value");
  await page.waitForURL(`**/orgs/${org}/my-tools*`, { timeout: 10_000 });

  const landed = new URL(page.url());
  expect(landed.pathname).toBe(`/orgs/${org}/my-tools`);
  expect(
    landed.searchParams.get("reauth"),
    `The redirect dropped the ?reauth= token. The email's one-click ` +
      `re-auth link depends on it surviving the hop.`
  ).toBe("test-token-value");
});
