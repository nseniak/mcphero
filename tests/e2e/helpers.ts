import { test, type Page, type APIRequestContext } from "@playwright/test";
import { Client, type ClientOptions } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

// Per-shard URLs are injected by the Python orchestrator
// (tests/run-e2e-tests.py) so each Playwright process talks to its
// own backend + MCP fakes instead of trampling another shard's state.
// Defaults match the historic single-shard ports so a stale invocation
// without env vars still hits the same endpoints a developer is used
// to. They name 127.0.0.1, the address those servers bind (see
// LOOPBACK_HOST in tests/run-e2e-tests.py); the OAuth fake advertises
// 127.0.0.1, and the MCP SDK rejects a resource on another host.
export const BACKEND_URL =
  process.env.E2E_BACKEND_URL ?? "http://127.0.0.1:8080";
export const TEST_MCP_URL =
  process.env.E2E_TEST_MCP_URL ?? "http://127.0.0.1:9999";
export const OAUTH_TEST_MCP_URL =
  process.env.E2E_OAUTH_TEST_MCP_URL ?? "http://127.0.0.1:9998";

/** Host of the frontend the running test's pages load (its baseURL). */
export function frontendHost(): string {
  return new URL(test.info().project.use.baseURL ?? BACKEND_URL).hostname;
}

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

  // Copy the cookie onto the frontend's host. Cookies ignore the port,
  // so when frontend and backend share a host (always, under the
  // orchestrator) this just rewrites the same cookie. It matters when
  // they differ, e.g. pages on `localhost` against a 127.0.0.1 backend.
  const cookies = await context.cookies(BACKEND_URL);
  const sessionCookie = cookies.find((c) => c.name === "mcpolis_session");
  if (!sessionCookie) {
    throw new Error("dev-stub login did not set session cookie");
  }
  await context.addCookies([
    {
      ...sessionCookie,
      domain: frontendHost(),
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
 * Accept the invitation to ``orgSlug`` as the person ``request`` is
 * signed in as, then open the org, as the Join button does. Inviting
 * someone doesn't make them a member: until they accept, they have no
 * access to the org.
 */
export async function acceptInvitation(
  request: APIRequestContext,
  orgSlug: string,
) {
  const resp = await request.post(
    `${BACKEND_URL}/api/invitations/${encodeURIComponent(orgSlug)}/accept`,
  );
  if (resp.status() !== 200) {
    throw new Error(
      `accept invitation failed: ${resp.status()} ${await resp.text()}`,
    );
  }
  const switched = await request.post(
    `${BACKEND_URL}/api/orgs/${encodeURIComponent(orgSlug)}/switch`,
  );
  if (switched.status() !== 204) {
    throw new Error(
      `switch after joining failed: ${switched.status()} ${await switched.text()}`,
    );
  }
}

/**
 * Sign ``request`` in as ``email`` and accept their invitation to
 * ``orgSlug``. Leaves ``request`` signed in as ``email``.
 */
export async function joinAs(
  request: APIRequestContext,
  email: string,
  orgSlug: string,
) {
  await apiLoginAs(request, email);
  await acceptInvitation(request, orgSlug);
}

/** Everyone the e2e seed lets sign in to the OAuth test upstreams. */
const OAUTH_SIGN_IN_HOLDERS = [
  "admin@example.com",
  "admin2@example.com",
  "alice@example.com",
];

/**
 * Put an OAuth upstream back to "stopped, nobody signed in": the state
 * that shows Authenticate. The admin Stop keeps every saved sign-in, so
 * each possible holder signs themselves out first. Leaves ``request``
 * logged in as admin@example.com.
 */
export async function resetOAuthUpstream(
  request: APIRequestContext,
  upstreamId: string,
) {
  for (const email of OAUTH_SIGN_IN_HOLDERS) {
    await apiLoginAs(request, email);
    await request.post(`${BACKEND_URL}/api/auth/disconnect/${upstreamId}`);
  }
  await apiLoginAs(request, "admin@example.com");
  await request.post(
    `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/disconnect`,
  );
}

/**
 * Start an OAuth upstream with nobody signed in: admin@example.com signs
 * in through the admin tab (an admin's first sign-in is what starts a
 * sign-in MCP), then signs out again. Personal sign-ins on /my-tools
 * are refused while the upstream is stopped, and the e2e seed adds it
 * stopped. Leaves ``request`` logged in as admin@example.com.
 */
export async function startOAuthUpstreamSignedOut(
  request: APIRequestContext,
  upstreamId: string,
) {
  const admin = "admin@example.com";
  await apiLoginAs(request, admin);
  const connect = await request.post(
    `${BACKEND_URL}/api/admin/upstreams/${upstreamId}/connect`,
  );
  if (connect.status() !== 200) {
    throw new Error(`admin connect failed: ${connect.status()} ${await connect.text()}`);
  }
  const body = await connect.json();
  if (!body.connected) {
    const authorizeUrl = new URL(body.authorization_url);
    authorizeUrl.searchParams.set("email", admin);
    const authorize = await request.get(authorizeUrl.toString(), {
      maxRedirects: 0,
    });
    await request.get(authorize.headers()["location"], { maxRedirects: 0 });
    await new Promise((r) => setTimeout(r, 200));
  }
  await request.post(`${BACKEND_URL}/api/auth/disconnect/${upstreamId}`);
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
  return makeMcpClientAtPath(token, path, options);
}

/**
 * Like ``makeMcpClient`` but with an explicit gateway path — used by
 * specs that need the slug-scoped user mount (``mcp/{slug}/``), e.g.
 * the service-token org-pinning checks.
 */
export async function makeMcpClientAtPath(
  token: string,
  path: string,
  options: ClientOptions = {}
): Promise<Client> {
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
