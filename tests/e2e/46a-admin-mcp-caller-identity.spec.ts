/**
 * The Admin MCP knows who is calling it.
 *
 * An admin's assistant reaches ``/admin-mcp/<slug>/`` with a bearer
 * token, never with the dashboard cookie. The admin tools must still
 * act as that admin: a sign-in started by ``connect_upstream`` is
 * saved under the admin's email, so the dashboard shows the admin as
 * the slot owner afterwards.
 *
 * Shares the seeded ``oauth-tools`` server (admin sign-in) and the fake
 * OAuth provider with 15-admin-oauth-takeover; ``beforeEach`` resets
 * both, the same way that spec does: every sign-in removed, stopped.
 */
import { test, expect } from "@playwright/test";

import { apiLoginAs, OAUTH_TEST_MCP_URL, resetOAuthUpstream } from "./helpers";
import {
  ADMIN,
  callAdminTool,
  finishSignIn,
  slotOwner,
} from "./_admin_mcp_helpers";

const UPSTREAM = "oauth-tools";

test.beforeEach(async ({ request }) => {
  await request.post(`${OAUTH_TEST_MCP_URL}/test/reset`);
  await resetOAuthUpstream(request, UPSTREAM);
});

test("connect_upstream over a bearer token signs in as the calling admin", async ({
  request,
}) => {
  await apiLoginAs(request, ADMIN);
  expect(await slotOwner(request, UPSTREAM)).toBeNull();

  const text = await callAdminTool(request, ADMIN, "connect_upstream", {
    mcp_id: UPSTREAM,
  });
  const link = text.match(/https?:\/\/\S+/);
  expect(link, `no sign-in link in: ${text}`).toBeTruthy();
  await finishSignIn(request, link![0], ADMIN);

  await expect.poll(() => slotOwner(request, UPSTREAM)).toBe(ADMIN);

  // Signed in now: connecting again discovers the MCP's tools at once.
  const again = await callAdminTool(request, ADMIN, "connect_upstream", {
    mcp_id: UPSTREAM,
  });
  expect(again).toMatch(/is connected\. Discovered [1-9]\d* tools\./);
});
