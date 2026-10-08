/**
 * An invitation is accepted by the invited person, never by signing in.
 *
 * The admin adds an address on the Team page: that is an invitation.
 * The invited person becomes a member only when they click Join: on the
 * Join page the invite link opens, or on the invitation shown where they
 * land after signing in without the link. Until then they get no tools
 * from the org's gateway and can't open its pages; Decline deletes the
 * invitation. The letter case the admin typed the address in doesn't
 * matter.
 *
 * Co-location-safe: every invited address is unique to this run, and
 * every check names the org by its slug.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import {
  apiLoginAs,
  loginAs,
  makeMcpClientAtPath,
  mintMcpToken,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const ORG_NAME = "Acme Corp";
const ADMIN = "admin@example.com";

function uniqueEmail(name: string): string {
  const run = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  return `${name}-${run}@example.com`;
}

async function invite(request: APIRequestContext, email: string): Promise<void> {
  await apiLoginAs(request, ADMIN);
  const resp = await request.post(`${BACKEND}/api/admin/users`, {
    data: { email, role: "user" },
  });
  expect(resp.status()).toBe(201);
}

async function teamStatus(
  request: APIRequestContext,
  email: string,
): Promise<string | undefined> {
  await apiLoginAs(request, ADMIN);
  const users = (await (await request.get(`${BACKEND}/api/admin/users`)).json()) as Array<{
    email: string;
    status: string;
  }>;
  return users.find((u) => u.email === email)?.status;
}

async function removeQuietly(request: APIRequestContext, email: string) {
  await apiLoginAs(request, ADMIN);
  await request
    .delete(`${BACKEND}/api/admin/users/${encodeURIComponent(email)}`)
    .catch(() => {});
}

/** How many tools ``email``'s AI client gets from the org's gateway URL. */
async function gatewayToolCount(
  request: APIRequestContext,
  email: string,
): Promise<number> {
  const token = await mintMcpToken(request, email, ORG);
  const client = await makeMcpClientAtPath(token, `mcp/${ORG}/`);
  try {
    return (await client.listTools()).tools.length;
  } finally {
    await client.close().catch(() => {});
  }
}

test("an invited person joins by clicking Join on the invite page", async ({
  page,
  request,
}) => {
  const invitee = uniqueEmail("joiner");
  await invite(request, invitee);
  try {
    // Signed in, but only invited: no tools, no pages of the org.
    await loginAs(page, invitee);
    expect(await gatewayToolCount(request, invitee)).toBe(0);
    expect(
      (await page.request.get(`${BACKEND}/api/user/mcps`)).status(),
    ).toBe(403);
    expect(await teamStatus(request, invitee)).toBe("pending");

    await page.goto(`/orgs/${ORG}/join`);
    await expect(
      page.getByRole("heading", { name: `Join ${ORG_NAME}` }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Join", exact: true }).click();

    // Joined: the person lands in the org.
    await page.waitForURL(new RegExp(`/orgs/${ORG}/`), { timeout: 15_000 });
    expect(await teamStatus(request, invitee)).toBe("active");
    expect(await gatewayToolCount(request, invitee)).toBeGreaterThan(0);
  } finally {
    await removeQuietly(request, invitee);
  }
});

test("signing in without the link shows the invitation, and Decline deletes it", async ({
  page,
  request,
}) => {
  const invitee = uniqueEmail("decliner");
  await invite(request, invitee);
  try {
    await loginAs(page, invitee);
    // No org yet: they land on the page offering to create one, which
    // lists their invitation first.
    await page.goto("/app");
    const invitations = page.getByTestId("pending-invitations");
    await expect(invitations).toContainText(
      `${ORG_NAME} invited you to join as user.`,
    );
    expect(await teamStatus(request, invitee)).toBe("pending");

    await invitations.getByRole("button", { name: "Decline" }).click();

    await expect(page.getByTestId("pending-invitations")).toHaveCount(0, {
      timeout: 10_000,
    });
    expect(await teamStatus(request, invitee)).toBeUndefined();
  } finally {
    await removeQuietly(request, invitee);
  }
});

test("an invitation typed with capitals is joined by the same address in lower case", async ({
  page,
  request,
}) => {
  // The admin types capitals; the sign-in reports the address in lower
  // case. Letter case carries no meaning in an address.
  const invitee = uniqueEmail("capitals");
  const typed = invitee.charAt(0).toUpperCase() + invitee.slice(1).replace(
    "@example.com", "@Example.com",
  );
  await invite(request, typed);
  try {
    await loginAs(page, invitee);
    await page.goto(`/orgs/${ORG}/join`);
    await expect(
      page.getByRole("heading", { name: `Join ${ORG_NAME}` }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Join", exact: true }).click();

    await page.waitForURL(new RegExp(`/orgs/${ORG}/`), { timeout: 15_000 });
    // The Team page keeps the address as the admin typed it.
    expect(await teamStatus(request, typed)).toBe("active");
    expect(await gatewayToolCount(request, invitee)).toBeGreaterThan(0);
  } finally {
    await removeQuietly(request, typed);
  }
});
