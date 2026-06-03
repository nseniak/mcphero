/**
 * Shared helpers for the 21*-start-clears-stale-error-and-logs split.
 *
 * Each spec keeps its own beforeEach/afterEach so a stray leftover
 * upstream from one test can't poison another. The shared bits are
 * just the constants + the polling/fetch helpers.
 */
import { expect, type APIRequestContext } from "@playwright/test";

import { apiLoginAs, BACKEND_URL } from "./helpers";

export { loginAs, BACKEND_URL } from "./helpers";

export const ORG = "acme-corp";
export const ADMIN = "admin@example.com";
export const UPSTREAM_ID = "bogus-mcp-21";

export async function adminApi(request: APIRequestContext): Promise<APIRequestContext> {
  await apiLoginAs(request, ADMIN);
  return request;
}

export interface UpstreamDetail {
  starting: boolean;
  ready: boolean;
  disconnect_reason: string | null;
}

export async function fetchDetail(
  api: APIRequestContext,
  upstreamId: string,
): Promise<UpstreamDetail> {
  const resp = await api.get(`${BACKEND_URL}/api/admin/upstreams/${upstreamId}`);
  expect(resp.status()).toBe(200);
  return (await resp.json()) as UpstreamDetail;
}

export async function waitForCondition(
  predicate: () => Promise<boolean>,
  timeoutMs: number,
  pollMs = 100,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return true;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return predicate();
}
