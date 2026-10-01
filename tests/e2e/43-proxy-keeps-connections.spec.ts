import { test, expect } from "@playwright/test";

/**
 * Regression: Vite's dev proxy must keep the browser's connection open.
 *
 * Without a keep-alive agent, Vite's proxy sends every backend request
 * with `Connection: close` and passes the backend's close on to the
 * browser, so each API call opens two new TCP connections (browser ->
 * Vite, Vite -> backend). Under the 4-shard e2e run that churn made
 * macOS stall every new loopback connection for seconds: the
 * `ERR_SOCKET_NOT_CONNECTED` / `connect ECONNREFUSED ::1` flakes. See
 * `backendAgent` in frontend/vite.config.ts; 44-vite-proxy-pool.spec.ts
 * checks the Vite -> backend side.
 */
test("proxied API responses keep the browser connection open", async ({ request }) => {
  // Relative URL: served by this shard's Vite, which proxies /api.
  const resp = await request.get("/api/config/features");
  expect(resp.status()).toBe(200);
  // The backend answered, not Vite's SPA fallback (also a 200 keep-alive).
  expect(resp.headers()["content-type"]).toContain("application/json");
  expect(await resp.json()).toHaveProperty("mode");
  expect(resp.headers()["connection"]).toBe("keep-alive");
});
