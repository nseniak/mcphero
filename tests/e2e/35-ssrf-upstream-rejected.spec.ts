/**
 * Security finding F-01: SSRF on user-supplied upstream URLs.
 *
 * Org admins must NOT be able to register an HTTP MCP whose URL points
 * at the EC2 instance metadata service or any private/loopback range.
 * The endpoint must reject those URLs at create time with a 400 +
 * ``UNSAFE_UPSTREAM_URL`` code, so the operator sees a clean error
 * rather than a hung "connecting…" or a 500.
 *
 * A public URL (here: ``https://1.1.1.1/mcp``, an IP literal so the
 * SSRF check skips DNS via ``AI_NUMERICHOST`` — keeps the test
 * passing on networks whose DNS filters out ``example.com``) must
 * still be
 * accepted — we're closing the SSRF, not breaking legitimate
 * upstreams.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import { apiLoginAs, BACKEND_URL as BACKEND } from "./helpers";

const ADMIN = "admin@example.com";
const UPSTREAM_ID_BAD_IMDS = "ssrf-imds-35";
const UPSTREAM_ID_BAD_PRIVATE = "ssrf-priv-35";
const UPSTREAM_ID_PUBLIC = "ssrf-public-35";

// The e2e harness sets MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1 so the
// per-shard test MCP server on 127.0.0.1 can register; we therefore
// don't probe a loopback URL here (it'd be accepted by design in
// dev/e2e). The IMDS literal and an RFC1918 private IP are still
// blocked unconditionally — those are the actual SSRF targets.

async function adminApi(request: APIRequestContext): Promise<APIRequestContext> {
  await apiLoginAs(request, ADMIN);
  return request;
}

async function cleanup(api: APIRequestContext, ids: string[]): Promise<void> {
  for (const id of ids) {
    await api.delete(`${BACKEND}/api/admin/upstreams/${id}`);
  }
}

test.describe("SSRF: upstream-create rejects private URLs", () => {
  test.beforeAll(async ({ request }) => {
    const api = await adminApi(request);
    await cleanup(api, [
      UPSTREAM_ID_BAD_IMDS,
      UPSTREAM_ID_BAD_PRIVATE,
      UPSTREAM_ID_PUBLIC,
    ]);
  });

  test.afterAll(async ({ request }) => {
    const api = await adminApi(request);
    await cleanup(api, [
      UPSTREAM_ID_BAD_IMDS,
      UPSTREAM_ID_BAD_PRIVATE,
      UPSTREAM_ID_PUBLIC,
    ]);
  });

  test("rejects an IMDS URL with UNSAFE_UPSTREAM_URL", async ({
    request,
  }) => {
    const api = await adminApi(request);
    const resp = await api.post(`${BACKEND}/api/admin/upstreams`, {
      data: {
        id: UPSTREAM_ID_BAD_IMDS,
        display_name: "IMDS Probe",
        url: "http://169.254.169.254/latest/meta-data/",
        auth_mode: "service_account",
      },
    });
    expect(resp.status()).toBe(400);
    const body = await resp.json();
    // FastAPI HTTPException(detail=...) lands as either ``detail``
    // string or ``{detail: {code, message}}`` — accept both shapes.
    const detail = body.detail;
    const codeStr =
      typeof detail === "string"
        ? detail
        : (detail && (detail.code ?? detail.message)) ?? "";
    expect(JSON.stringify(body)).toContain("UNSAFE_UPSTREAM_URL");
    void codeStr;
  });

  test("rejects an RFC1918 URL with UNSAFE_UPSTREAM_URL", async ({
    request,
  }) => {
    const api = await adminApi(request);
    const resp = await api.post(`${BACKEND}/api/admin/upstreams`, {
      data: {
        id: UPSTREAM_ID_BAD_PRIVATE,
        display_name: "Private Probe",
        url: "http://10.0.0.5/mcp",
        auth_mode: "service_account",
      },
    });
    expect(resp.status()).toBe(400);
    const body = await resp.json();
    expect(JSON.stringify(body)).toContain("UNSAFE_UPSTREAM_URL");
  });

  test("accepts a public HTTPS URL", async ({ request }) => {
    const api = await adminApi(request);
    const resp = await api.post(`${BACKEND}/api/admin/upstreams`, {
      data: {
        id: UPSTREAM_ID_PUBLIC,
        display_name: "Public OK",
        url: "https://1.1.1.1/mcp",
        auth_mode: "service_account",
      },
    });
    expect([200, 201]).toContain(resp.status());
  });
});
