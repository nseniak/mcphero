import { test, expect, type APIRequestContext, type Page } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import {
  apiLoginAs,
  createOrg,
  loginAs,
  makeMcpClient,
  makeMcpClientAtPath,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

/**
 * Service tokens end-to-end: dashboard CRUD (shown-once UX), gateway
 * connection with the minted bearer, role-scoped tool access, audit
 * identity, org pinning, and revocation.
 *
 * Isolation guarantees this spec relies on:
 * - Cross-run: the orchestrator gives every run a fresh Mongo DB
 *   (``mcpolis_e2e_<token>_sN``), so leftovers from a crashed prior
 *   run are unreachable by construction.
 * - Within-run: tests run serially (workers: 1) in file order, and
 *   each test's opening ``revokeQuietly`` covers Playwright's
 *   ``retries: 1`` re-entering a test that died between create and
 *   revoke.
 */

async function adminApi(request: APIRequestContext) {
  await apiLoginAs(request, ADMIN);
  return request;
}

/** Revoke quietly — cleanup helper, 404 is fine. */
async function revokeQuietly(api: APIRequestContext, label: string) {
  await api
    .delete(`${BACKEND}/api/admin/service-tokens/${label}`)
    .catch(() => {});
}

/** Drive the UI create form and return the shown-once raw token. */
async function createTokenViaUi(
  page: Page,
  label: string,
  role: string,
): Promise<string> {
  await page.goto(`/orgs/${ORG}/admin/service-tokens`);
  await page.getByRole("button", { name: "New token" }).click();
  await page.getByPlaceholder("e.g. ci-bot").fill(label);
  await page.getByRole("combobox").selectOption(role);
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const tokenValue = await page
    .getByTestId("service-token-value")
    .textContent();
  expect(tokenValue).toBeTruthy();
  expect(tokenValue!).toMatch(/^svct_/);
  return tokenValue!;
}

test.describe("Service tokens", () => {
  let client: Client | null = null;

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("create in UI, connect, call tools per role, audit, revoke", async ({
    page,
    request,
  }) => {
    const api = await adminApi(request);
    await revokeQuietly(api, "e2e-bot");

    await loginAs(page, ADMIN, ORG);
    const token = await createTokenViaUi(page, "e2e-bot", "user");

    // Shown-once contract: the list view never reveals the value.
    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByTestId("service-token-value")).toHaveCount(0);
    await expect(page.getByText("e2e-bot")).toBeVisible();
    const listResp = await api.get(`${BACKEND}/api/admin/service-tokens`);
    expect(await listResp.text()).not.toContain(token);

    // Bare /mcp/: the org pin resolves the token's org — tools come
    // back in single-org form (no ``{slug}__`` prefix, unlike the
    // human multi-org merge mode).
    client = await makeMcpClient(token, ORG, "mcp");
    const bareNames = (await client.listTools()).tools.map((t) => t.name);
    expect(bareNames).toContain("test-tools__echo");
    await client.close();
    client = null;

    // Slug-scoped URL (the documented form) works too.
    client = await makeMcpClientAtPath(token, `mcp/${ORG}/`);
    const names = (await client.listTools()).tools.map((t) => t.name);
    expect(names).toContain("test-tools__echo");

    const echoResp = await client.callTool({
      name: "test-tools__echo",
      arguments: { message: "svc-hello" },
    });
    expect(JSON.stringify(echoResp.content)).toContain("svc-hello");

    // The call is audited under the svc identity.
    const auditResp = await api.get(
      `${BACKEND}/api/admin/audit?tool=test-tools__echo&user_id=svc:e2e-bot`,
    );
    expect(auditResp.status()).toBe(200);
    const audit = await auditResp.json();
    const entry = audit.entries.find(
      (e: { user_id?: string }) => e.user_id === "svc:e2e-bot",
    );
    expect(entry).toBeDefined();

    // Audit page shows the svc identity.
    await page.goto(`/orgs/${ORG}/admin/audit`);
    await expect(page.getByText("svc:e2e-bot").first()).toBeVisible();

    // Revoke through the UI (trash icon in the token's row + confirm).
    await page.goto(`/orgs/${ORG}/admin/service-tokens`);
    await page
      .locator("tr", { hasText: "e2e-bot" })
      .getByRole("button")
      .click();
    await page.getByRole("button", { name: "Revoke", exact: true }).click();
    await expect(page.getByText("No service tokens yet")).toBeVisible();

    // Revocation bites on the next connection.
    await client.close();
    client = null;
    await expect(
      makeMcpClientAtPath(token, `mcp/${ORG}/`),
    ).rejects.toThrow();
  });

  test("restricted role sees no tools and is denied calls", async ({
    request,
  }) => {
    const api = await adminApi(request);
    await revokeQuietly(api, "e2e-restricted");

    // A fresh role starts with no MCP access — exactly the
    // least-privilege shape the docs recommend for bots.
    const roleResp = await api.post(`${BACKEND}/api/admin/roles`, {
      data: { name: "svc-locked" },
    });
    expect([201, 409]).toContain(roleResp.status());

    const mintResp = await api.post(
      `${BACKEND}/api/admin/service-tokens`,
      { data: { label: "e2e-restricted", role: "svc-locked" } },
    );
    expect(mintResp.status()).toBe(201);
    const minted = await mintResp.json();

    client = await makeMcpClientAtPath(minted.token, `mcp/${ORG}/`);
    const names = (await client.listTools()).tools.map((t) => t.name);
    expect(names).toHaveLength(0);

    const resp = await client.callTool({
      name: "test-tools__echo",
      arguments: { message: "nope" },
    });
    expect(JSON.stringify(resp.content)).toContain("Access denied");

    await revokeQuietly(api, "e2e-restricted");
  });

  test("token is pinned to its org — other org slugs are rejected", async ({
    request,
  }) => {
    const api = await adminApi(request);
    await revokeQuietly(api, "e2e-pinned");

    const mintResp = await api.post(
      `${BACKEND}/api/admin/service-tokens`,
      { data: { label: "e2e-pinned", role: "user" } },
    );
    expect(mintResp.status()).toBe(201);
    const minted = await mintResp.json();

    // A second org the token must NOT be able to reach.
    await createOrg(
      request,
      "pin-other-admin@example.com",
      "svc-pin-other",
      "Pin Other",
    );

    await expect(
      makeMcpClientAtPath(minted.token, "mcp/svc-pin-other/"),
    ).rejects.toThrow();

    // Back as the org admin (createOrg switched the cookie identity).
    const api2 = await adminApi(request);
    await revokeQuietly(api2, "e2e-pinned");
  });

  test("duplicate label is rejected with 409", async ({ request }) => {
    const api = await adminApi(request);
    await revokeQuietly(api, "e2e-dup");

    const first = await api.post(`${BACKEND}/api/admin/service-tokens`, {
      data: { label: "e2e-dup", role: "user" },
    });
    expect(first.status()).toBe(201);
    const second = await api.post(`${BACKEND}/api/admin/service-tokens`, {
      data: { label: "e2e-dup", role: "user" },
    });
    expect(second.status()).toBe(409);

    await revokeQuietly(api, "e2e-dup");
  });
});
