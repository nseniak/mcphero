import {
  test,
  expect,
  type APIRequestContext,
} from "@playwright/test";
import { BACKEND_URL, createOrg } from "./helpers";

/**
 * Sign-in via ``/api/auth/login?join={slug}`` must stamp the session
 * cookie with that slug, even when the user is also a member of
 * other orgs (so the default "first membership" fallback would have
 * picked a different one).
 *
 * Source of the override: dashboard_auth.py callback line ~357
 * (``if join_slug: chosen_slug = join_slug``). The join slug is
 * stashed against the OAuth state at /api/auth/login time and read
 * back from ``_pending_logins`` in the callback.
 */

const stamp = Date.now().toString(36);
const ADMIN = `admin-join-${stamp}@test.com`;
const USER = `user-join-${stamp}@test.com`;
const ORG_A = `orga-join-${stamp}`;
const ORG_B = `orgb-join-${stamp}`;

/** Walk the dev-stub OAuth flow with an explicit ``join`` slug. */
async function devStubLoginWithJoin(
  request: APIRequestContext,
  email: string,
  joinSlug: string,
): Promise<void> {
  const startResp = await request.get(
    `${BACKEND_URL}/api/auth/login?join=${encodeURIComponent(joinSlug)}`,
    { maxRedirects: 0 },
  );
  expect(startResp.status()).toBe(307);
  const pickerLocation = startResp.headers()["location"];
  const stateMatch = pickerLocation.match(/[?&]state=([^&]+)/);
  if (!stateMatch) throw new Error(`no state in ${pickerLocation}`);
  const state = decodeURIComponent(stateMatch[1]);

  const submitResp = await request.get(
    `${BACKEND_URL}/api/auth/dev-stub/submit`,
    {
      params: {
        email,
        state,
        redirect_uri: `${BACKEND_URL}/api/auth/callback`,
      },
      maxRedirects: 0,
    },
  );
  expect(submitResp.status()).toBe(302);
  const callbackPath = submitResp.headers()["location"];
  const callbackResp = await request.get(`${BACKEND_URL}${callbackPath}`, {
    maxRedirects: 0,
  });
  expect(callbackResp.status()).toBe(302);
}

test("?join={slug} on sign-in lands the cookie on that org for a multi-org user", async ({
  playwright,
  request,
}) => {
  // ── Setup: admin creates ORG_A and ORG_B, invites user@ to both.
  await createOrg(request, ADMIN, ORG_A, "Org A");
  let resp = await request.post(`${BACKEND_URL}/api/orgs/${ORG_A}/switch`);
  expect(resp.status()).toBe(204);
  resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: USER, role: "user" },
  });
  expect([200, 201]).toContain(resp.status());

  await createOrg(request, ADMIN, ORG_B, "Org B");
  resp = await request.post(`${BACKEND_URL}/api/orgs/${ORG_B}/switch`);
  expect(resp.status()).toBe(204);
  resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: USER, role: "user" },
  });
  expect([200, 201]).toContain(resp.status());

  // ── Sign in twice from independent contexts, once per direction.
  // Asserting both directions proves the join slug is honored
  // regardless of whatever the default ``orgs[0]`` would have been.
  for (const target of [ORG_A, ORG_B]) {
    const ctx = await playwright.request.newContext();
    try {
      await devStubLoginWithJoin(ctx, USER, target);
      const meResp = await ctx.get(`${BACKEND_URL}/api/auth/me`);
      expect(meResp.status()).toBe(200);
      const me = (await meResp.json()) as {
        current_org: { slug: string } | null;
      };
      expect(
        me.current_org?.slug,
        `?join=${target} should pin the cookie to ${target}`,
      ).toBe(target);
    } finally {
      await ctx.dispose();
    }
  }
});
