/**
 * An org admin can revoke the gateway sign-in of a member of their own
 * org only (Team page → "Disconnect from gateway",
 * ``DELETE /api/admin/gateway/users/{email}``).
 *
 * A gateway sign-in belongs to the person, not the org: revoking it
 * signs them out of every org they're in. Before the fix, any org admin
 * could revoke anyone's sign-in by email, including someone who only
 * belongs to another org. Now the admin gets the same 404 as for
 * "no tokens", and the outsider keeps working.
 *
 * Co-location-safe: the outsider and their org are created by this
 * test with unique names; no shared seed user is touched.
 */
import { test, expect } from "@playwright/test";

import {
  apiLoginAs,
  createOrg,
  mintMcpToken,
  makeMcpClientAtPath,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ADMIN = "admin@example.com"; // admin of acme-corp only
const stamp = Date.now().toString(36);
const OUTSIDER = `outsider-revoke-${stamp}@test.com`;
const OUTSIDER_ORG = `out-rv-${stamp}`;

/** True iff ``token`` still opens the outsider's gateway and lists tools. */
async function gatewayWorks(token: string): Promise<boolean> {
  try {
    const client = await makeMcpClientAtPath(token, `mcp/${OUTSIDER_ORG}/`);
    try {
      await client.listTools();
      return true;
    } finally {
      await client.close().catch(() => {});
    }
  } catch {
    return false;
  }
}

test("an org admin cannot revoke the gateway sign-in of someone outside their org", async ({
  request,
}) => {
  // The outsider runs their own org and signs in to the gateway.
  await createOrg(request, OUTSIDER, OUTSIDER_ORG, "Outsider Org");
  const token = await mintMcpToken(request, OUTSIDER, OUTSIDER_ORG);
  expect(await gatewayWorks(token)).toBe(true);

  // The acme-corp admin tries to revoke the outsider's sign-in.
  await apiLoginAs(request, ADMIN);
  const revoke = await request.delete(
    `${BACKEND}/api/admin/gateway/users/${encodeURIComponent(OUTSIDER)}`,
  );
  expect(revoke.status()).toBe(404);

  // The outsider is still signed in.
  expect(await gatewayWorks(token)).toBe(true);
});

test("inviting an outsider first gives the admin no power over them", async ({
  request,
}) => {
  // An invitation is not a membership: until the outsider accepts it,
  // neither revoking their sign-in nor removing the invitation touches
  // their gateway sign-in, and the answer tells nothing about them.
  const stamp2 = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const outsider = `outsider-invited-${stamp2}@test.com`;
  const outsiderOrg = `out-inv-${stamp2}`.slice(0, 20);
  await createOrg(request, outsider, outsiderOrg, "Invited Outsider Org");
  const token = await mintMcpToken(request, outsider, outsiderOrg);
  const works = async () => {
    try {
      const client = await makeMcpClientAtPath(token, `mcp/${outsiderOrg}/`);
      try {
        await client.listTools();
        return true;
      } finally {
        await client.close().catch(() => {});
      }
    } catch {
      return false;
    }
  };
  expect(await works()).toBe(true);

  await apiLoginAs(request, ADMIN);
  const invited = await request.post(`${BACKEND}/api/admin/users`, {
    data: { email: outsider, role: "user" },
  });
  expect(invited.status()).toBe(201);

  const revoke = await request.delete(
    `${BACKEND}/api/admin/gateway/users/${encodeURIComponent(outsider)}`,
  );
  expect(revoke.status()).toBe(404);
  const removed = await request.delete(
    `${BACKEND}/api/admin/users/${encodeURIComponent(outsider)}`,
  );
  expect(removed.status()).toBe(200);

  expect(await works()).toBe(true);
});
