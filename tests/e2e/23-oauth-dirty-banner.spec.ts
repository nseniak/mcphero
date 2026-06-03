/**
 * Editing the JSON config of an OAuth-mode upstream must mark it
 * dirty (``is_dirty=true``) and surface the ``DirtyConfigBanner``,
 * just like service_account upstreams do.
 *
 * The bug: ``is_dirty`` is gated on
 * ``UpstreamState.started_config_hash``, which is only set when a
 * connection task is created (``connect_shared`` /
 * ``connect_admin_session``). For OAuth modes:
 *   - ``ready`` is computed purely from token existence in
 *     ``connection_store`` ([_deps.py:163-180]) — no UpstreamState
 *     involvement.
 *   - ``connect_admin_session`` only fires lazily on the first
 *     gateway tool call, NOT on the dashboard's
 *     ``Authenticate`` flow nor on the OAuth callback itself.
 *   - So an admin can authenticate, see ``ready=true``, edit the
 *     config, and watch ``is_dirty`` stay ``false`` forever — the
 *     baseline was never recorded.
 *
 * The fix persists ``started_config_hash`` alongside the OAuth
 * token (snapshot at token-set time) so the dashboard can compute
 * drift even before any session task has spun up.
 *
 * This test reproduces the bug end-to-end via the fake OAuth
 * provider at ``oauth_test_mcp_server.py``: drive a fresh OAuth
 * flow, edit the URL, assert dirty fires.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import {
  apiLoginAs,
  loginAs,
  OAUTH_TEST_MCP_URL,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

function uniqueId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

interface UpstreamDetail {
  ready: boolean;
  is_dirty: boolean;
  config_hash: string;
  auth_mode: string;
  starting: boolean;
  slot_owner: string | null;
  server_config: Record<string, unknown>;
}

async function fetchDetail(
  api: APIRequestContext,
  id: string,
): Promise<UpstreamDetail> {
  const r = await api.get(`${BACKEND}/api/admin/upstreams/${id}`);
  expect(r.status()).toBe(200);
  return (await r.json()) as UpstreamDetail;
}

async function adminApi(request: APIRequestContext): Promise<APIRequestContext> {
  await apiLoginAs(request, ADMIN);
  return request;
}

/**
 * Drive the admin_oauth connect flow against the fake provider.
 * Lifted from spec 15 (admin-oauth-takeover) so this spec stays
 * self-contained.
 */
async function completeAdminOauthConnect(
  api: APIRequestContext,
  id: string,
  loggedInAs: string,
): Promise<void> {
  const connectResp = await api.post(
    `${BACKEND}/api/admin/upstreams/${id}/connect`,
  );
  expect(connectResp.status()).toBe(200);
  const body = await connectResp.json();
  if (body.connected) return;
  expect(body.authorization_url).toBeTruthy();

  const authorizeUrl = new URL(body.authorization_url);
  authorizeUrl.searchParams.set("email", loggedInAs);
  const authorizeResp = await api.get(authorizeUrl.toString(), {
    maxRedirects: 0,
  });
  expect(authorizeResp.status()).toBe(302);
  const callbackLoc = authorizeResp.headers()["location"];
  expect(callbackLoc).toContain("/api/oauth/upstream/callback");

  const callbackResp = await api.get(callbackLoc, { maxRedirects: 0 });
  expect(callbackResp.status()).toBe(200);

  // Token-store write + tool re-discovery race; a 200ms grace is
  // what spec 15 uses, copied verbatim.
  await new Promise((r) => setTimeout(r, 200));
}

test.describe("Dirty banner fires for OAuth upstreams after config edit", () => {
  test("admin_oauth: API exposes is_dirty=true after a JSON config edit", async ({
    request,
  }) => {
    const api = await adminApi(request);
    const id = uniqueId("oauth-dirty");
    try {
      // Create our own admin_oauth upstream wired to the fake
      // provider — keeps this spec independent of the seeded
      // ``oauth-tools`` upstream other specs trample.
      const create = await api.post(`${BACKEND}/api/admin/upstreams`, {
        data: {
          id,
          display_name: `OAuth dirty ${id}`,
          url: `${OAUTH_TEST_MCP_URL}/mcp`,
          auth_mode: "admin_oauth",
          client_id: "e2e-client",
          client_secret: "e2e-secret",
        },
      });
      expect([200, 201]).toContain(create.status());

      // Authenticate so ready flips true.
      await completeAdminOauthConnect(api, id, ADMIN);
      const initial = await fetchDetail(api, id);
      expect(initial.ready).toBe(true);
      expect(initial.slot_owner).toBe(ADMIN);
      expect(initial.is_dirty).toBe(false);
      const baselineHash = initial.config_hash;

      // Edit the JSON config — change URL to a different (still
      // syntactically valid) value. The running session won't be
      // touched (per the unified save behaviour from
      // commit ``upstream-detail(ux): never auto-disconnect on save``).
      const put = await api.put(`${BACKEND}/api/admin/upstreams/${id}`, {
        data: {
          server_config: { url: `${OAUTH_TEST_MCP_URL}/mcp?edited=1` },
        },
      });
      expect(put.status()).toBe(200);

      const after = await fetchDetail(api, id);
      // Hash must have changed (sanity — edits the URL).
      expect(after.config_hash).not.toBe(baselineHash);
      // Ready must still be true (token still present).
      expect(after.ready).toBe(true);
      // The bug: this used to be false. After the fix it MUST be true.
      expect(
        after.is_dirty,
        "is_dirty must fire after a config edit on a ready OAuth upstream",
      ).toBe(true);
    } finally {
      await api.delete(`${BACKEND}/api/admin/upstreams/${id}`);
    }
  });

  test("admin_oauth: UI banner appears after a JSON config edit", async ({
    page,
    request,
  }) => {
    const api = await adminApi(request);
    const id = uniqueId("oauth-dirty-ui");
    try {
      await api.post(`${BACKEND}/api/admin/upstreams`, {
        data: {
          id,
          display_name: `OAuth dirty UI ${id}`,
          url: `${OAUTH_TEST_MCP_URL}/mcp`,
          auth_mode: "admin_oauth",
          client_id: "e2e-client",
          client_secret: "e2e-secret",
        },
      });
      await completeAdminOauthConnect(api, id, ADMIN);

      await loginAs(page, ADMIN, ORG);
      await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
      // Pre-edit: banner should be hidden.
      await expect(
        page.getByText(/Configuration has been modified/),
      ).toHaveCount(0);

      // Edit JSON config via the API (avoids fighting the wizard's
      // controlled-input validators in this spec; the UI assertion
      // is on the post-save dashboard, not the edit form).
      const put = await api.put(`${BACKEND}/api/admin/upstreams/${id}`, {
        data: {
          server_config: { url: `${OAUTH_TEST_MCP_URL}/mcp?edited=1` },
        },
      });
      expect(put.status()).toBe(200);

      // Reload the detail page; banner must surface within a few
      // seconds (post-save policy_change → frontend refetch).
      await page.reload();
      await expect(
        page.getByText(
          /Configuration has been modified\. Changes won't take effect/,
        ),
      ).toBeVisible({ timeout: 5_000 });
    } finally {
      await api.delete(`${BACKEND}/api/admin/upstreams/${id}`);
    }
  });
});
