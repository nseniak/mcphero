import { type Page, type APIRequestContext } from "@playwright/test";
import { Client, type ClientOptions } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

// Per-shard URLs are injected by the Python orchestrator
// (tests/run-e2e-tests.py) so each Playwright process talks to its
// own backend + MCP fakes instead of trampling another shard's state.
// Defaults match the historic single-shard ports so a stale invocation
// without env vars still hits the same endpoints a developer is used
// to.
export const BACKEND_URL =
  process.env.E2E_BACKEND_URL ?? "http://localhost:8080";
export const TEST_MCP_URL =
  process.env.E2E_TEST_MCP_URL ?? "http://localhost:9999";
export const OAUTH_TEST_MCP_URL =
  process.env.E2E_OAUTH_TEST_MCP_URL ?? "http://localhost:9998";

/**
 * Walk the dev-stub dashboard OAuth flow end-to-end:
 *   GET /api/auth/login          → 307 to picker
 *   GET /api/auth/dev-stub/submit → 302 to /api/auth/callback
 *   GET /api/auth/callback        → 302 + Set-Cookie (mcpolis_session)
 *
 * After this returns the page's browser context has a real signed
 * session cookie — the same code path production uses.
 *
 * ``orgSlug`` is unused in standalone mode (the cookie's org slug is
 * resolved from the user's memberships) but kept in the signature so
 * cloud-mode callers can later thread an explicit org through.
 */
export async function loginAs(
  page: Page,
  email: string,
  orgSlug: string = "default"
) {
  void orgSlug;
  const context = page.context();
  await runDevStubLogin(context.request, email);

  // Surface the cookie on the frontend origin too (Vite proxies /api
  // to the backend, but the browser cookie jar pairs by origin).
  const cookies = await context.cookies(BACKEND_URL);
  const sessionCookie = cookies.find((c) => c.name === "mcpolis_session");
  if (!sessionCookie) {
    throw new Error("dev-stub login did not set session cookie");
  }
  await context.addCookies([
    {
      ...sessionCookie,
      domain: "localhost",
      path: "/",
    },
  ]);
}

/**
 * Create an organization via the backend API.
 */
export async function createOrg(
  request: APIRequestContext,
  email: string,
  orgSlug: string,
  displayName: string
) {
  await runDevStubLogin(request, email);

  const resp = await request.post(`${BACKEND_URL}/api/orgs`, {
    data: { slug: orgSlug, display_name: displayName },
  });
  if (resp.status() !== 200 && resp.status() !== 201) {
    const text = await resp.text();
    // Ignore "already exists" errors
    if (!text.includes("already") && !text.includes("exists")) {
      throw new Error(`create org failed: ${resp.status()} ${text}`);
    }
  }
}

/**
 * Walk the dev-stub login flow on a Playwright APIRequestContext
 * (no browser). Use this in spec files that need an authenticated
 * request without rendering a page — the request context's cookie
 * jar will carry the signed session cookie afterwards.
 */
export async function apiLoginAs(
  request: APIRequestContext,
  email: string
) {
  await runDevStubLogin(request, email);
}

async function runDevStubLogin(
  request: APIRequestContext,
  email: string
) {
  // Don't auto-follow: each step's redirect carries information the
  // next step needs (the state token, the callback URL).
  const startResp = await request.get(`${BACKEND_URL}/api/auth/login`, {
    maxRedirects: 0,
  });
  if (startResp.status() !== 307) {
    throw new Error(
      `/api/auth/login expected 307, got ${startResp.status()} ${await startResp.text()}`
    );
  }
  const pickerLocation = startResp.headers()["location"];
  const stateMatch = pickerLocation.match(/[?&]state=([^&]+)/);
  if (!stateMatch) {
    throw new Error(`could not extract state from ${pickerLocation}`);
  }
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
    }
  );
  if (submitResp.status() !== 302) {
    throw new Error(
      `dev-stub submit expected 302, got ${submitResp.status()} ${await submitResp.text()}`
    );
  }
  const callbackPath = submitResp.headers()["location"];
  const callbackResp = await request.get(
    `${BACKEND_URL}${callbackPath}`,
    { maxRedirects: 0 }
  );
  if (callbackResp.status() !== 302) {
    throw new Error(
      `callback expected 302, got ${callbackResp.status()} ${await callbackResp.text()}`
    );
  }
}

/**
 * Mint a gateway bearer token via the test-only endpoint.
 * Requires MCPOLIS_TEST_MODE=1 on the backend (the e2e harness sets it).
 */
export async function mintMcpToken(
  request: APIRequestContext,
  email: string,
  orgSlug: string
): Promise<string> {
  const resp = await request.post(
    `${BACKEND_URL}/api/auth/test-mcp-token`,
    { data: { email, org_slug: orgSlug } }
  );
  if (resp.status() !== 200) {
    throw new Error(
      `test-mcp-token failed: ${resp.status()} ${await resp.text()}`
    );
  }
  const body = await resp.json();
  return body.access_token as string;
}

/**
 * Build an MCP streamable-HTTP client connected to the user gateway
 * (``/mcp/`` — fixed URL) or the admin MCP (``/admin-mcp/{slug}/``)
 * using the supplied bearer token. Caller is responsible for
 * ``await client.close()`` when done.
 *
 * ``orgSlug`` is required for the admin mount and ignored for the
 * user mount, since the user gateway aggregates across the
 * authenticated user's orgs without a slug in the URL.
 */
export async function makeMcpClient(
  token: string,
  orgSlug: string,
  mount: "mcp" | "admin-mcp" = "mcp",
  options: ClientOptions = {}
): Promise<Client> {
  const path = mount === "admin-mcp" ? `${mount}/${orgSlug}/` : `${mount}/`;
  const url = new URL(`${BACKEND_URL}/${path}`);
  const transport = new StreamableHTTPClientTransport(url, {
    requestInit: {
      headers: { Authorization: `Bearer ${token}` },
    },
  });
  const client = new Client({ name: "e2e-test", version: "0.0.1" }, options);
  await client.connect(transport);
  return client;
}

/**
 * Add a user to an org via the admin API.
 */
export async function addUserToOrg(
  request: APIRequestContext,
  adminEmail: string,
  orgSlug: string,
  userEmail: string,
  role: string = "user"
) {
  // Login as admin for this org
  await request.post(`${BACKEND_URL}/api/auth/test-login`, {
    data: { email: adminEmail, org_slug: orgSlug },
  });

  const resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: userEmail, role },
  });
  if (resp.status() !== 200 && resp.status() !== 201) {
    const text = await resp.text();
    if (!text.includes("already") && !text.includes("exists")) {
      throw new Error(`add user failed: ${resp.status()} ${text}`);
    }
  }
}
