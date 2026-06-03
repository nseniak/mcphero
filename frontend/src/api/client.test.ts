import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiFetch,
  ApiError,
  currentRouteOrgSlug,
  PlanLimitError,
  withOrgParam,
} from "./client";
import { setSessionExpiredHandler } from "../lib/session";

function makeResponse(status: number, body: unknown): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve(JSON.parse(text)),
  } as unknown as Response;
}

function stubFetch(status: number, body: unknown): void {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(makeResponse(status, body)));
}

const onExpired = vi.fn();

beforeEach(() => {
  onExpired.mockReset();
  setSessionExpiredHandler(onExpired);
});

afterEach(() => {
  setSessionExpiredHandler(null);
  vi.unstubAllGlobals();
  // Reset the route so org-slug derivation doesn't leak across tests.
  window.history.pushState({}, "", "/");
});

describe("apiFetch session-expiry wiring", () => {
  it("fires the session-expiry signal on a 401", async () => {
    stubFetch(401, { error: "Not authenticated" });
    await expect(apiFetch("/api/admin/upstreams")).rejects.toBeInstanceOf(ApiError);
    expect(onExpired).toHaveBeenCalledTimes(1);
  });

  it("throws an ApiError carrying the 401 status and message", async () => {
    stubFetch(401, { error: "Not authenticated" });
    await expect(apiFetch("/api/admin/upstreams")).rejects.toMatchObject({
      status: 401,
      message: "Not authenticated",
    });
  });

  it("does not fire the signal on a non-401 error", async () => {
    stubFetch(500, { error: "boom" });
    await expect(apiFetch("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(onExpired).not.toHaveBeenCalled();
  });

  it("does not fire the signal on a successful response", async () => {
    stubFetch(200, { ok: true });
    await expect(apiFetch("/api/x")).resolves.toEqual({ ok: true });
    expect(onExpired).not.toHaveBeenCalled();
  });

  it("does not fire the signal on a 402 plan-limit envelope", async () => {
    stubFetch(402, {
      error: "plan_limit_exceeded",
      gate: "upstreams",
      current: 1,
      limit: 1,
      message: "Upgrade required",
    });
    await expect(apiFetch("/api/x")).rejects.toBeInstanceOf(PlanLimitError);
    expect(onExpired).not.toHaveBeenCalled();
  });
});

describe("route org-slug derivation", () => {
  it("reads the slug from an /orgs/{slug}/... route", () => {
    window.history.pushState({}, "", "/orgs/acme/admin/upstream/up-1");
    expect(currentRouteOrgSlug()).toBe("acme");
  });

  it("decodes a percent-encoded slug", () => {
    window.history.pushState({}, "", "/orgs/a%20b/admin");
    expect(currentRouteOrgSlug()).toBe("a b");
  });

  it("returns null on slug-less routes", () => {
    window.history.pushState({}, "", "/admin/upstream/up-1");
    expect(currentRouteOrgSlug()).toBeNull();
  });

  it("returns null on /superadmin routes (uses superadmin endpoints)", () => {
    window.history.pushState({}, "", "/superadmin/orgs/acme");
    expect(currentRouteOrgSlug()).toBeNull();
  });

  it("appends ?org= when on an org route", () => {
    window.history.pushState({}, "", "/orgs/acme/admin");
    expect(withOrgParam("/api/events")).toBe("/api/events?org=acme");
  });

  it("uses & when the URL already has a query string", () => {
    window.history.pushState({}, "", "/orgs/acme/admin");
    expect(withOrgParam("/api/x?foo=1")).toBe("/api/x?foo=1&org=acme");
  });

  it("is a no-op on slug-less routes", () => {
    window.history.pushState({}, "", "/admin");
    expect(withOrgParam("/api/events")).toBe("/api/events");
  });
});

describe("apiFetch X-Org-Slug header", () => {
  it("sends X-Org-Slug derived from the /orgs/{slug}/ route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/orgs/acme/admin/upstream/up-1");

    await apiFetch("/api/admin/upstreams/up-1");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Org-Slug"]).toBe("acme");
  });

  it("omits X-Org-Slug on slug-less routes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/admin/upstream/up-1");

    await apiFetch("/api/admin/upstreams/up-1");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Org-Slug"]).toBeUndefined();
  });
});
