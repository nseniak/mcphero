/**
 * Tests for the client-side error reporter.
 *
 * Mirror of the contract pinned by
 * ``backend/tests/unit/test_client_errors_route.py``: the frontend
 * MUST send what the backend expects, and MUST never throw out of
 * its own handler (or it would mask the original error). We
 * exercise the public surface: ``reportClientError`` and the
 * ``installClientErrorReporter`` wire-up of ``window.error`` /
 * ``unhandledrejection``.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installClientErrorReporter, reportClientError } from "./clientErrorReporter";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function lastFetchBody(calls: FetchCall[]): Record<string, unknown> {
  expect(calls.length).toBeGreaterThan(0);
  const last = calls[calls.length - 1];
  expect(last.url).toBe("/api/client-errors");
  expect(last.init.method).toBe("POST");
  expect(last.init.keepalive).toBe(true);
  expect(last.init.credentials).toBe("include");
  expect((last.init.headers as Record<string, string>)["Content-Type"]).toBe(
    "application/json",
  );
  return JSON.parse(String(last.init.body)) as Record<string, unknown>;
}

let fetchCalls: FetchCall[] = [];

beforeEach(() => {
  fetchCalls = [];
  globalThis.fetch = vi.fn(async (input, init) => {
    fetchCalls.push({
      url: typeof input === "string" ? input : input.toString(),
      init: init ?? {},
    });
    return new Response(null, { status: 204 });
  }) as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reportClientError", () => {
  it("posts a client.reported_error envelope to /api/client-errors", () => {
    reportClientError({
      event: "client.reported_error",
      message: "signup.blocked",
      context: { reason: "slug_unavailable", slug: "athena-decision" },
    });
    const body = lastFetchBody(fetchCalls);
    expect(body.event).toBe("client.reported_error");
    // ``context`` is folded into ``message`` as JSON because the
    // backend schema has no structured ``context`` field — the whole
    // record stays grep-able from a single ``message`` query.
    expect(body.message).toContain("signup.blocked");
    expect(body.message).toContain("slug_unavailable");
    expect(body.message).toContain("athena-decision");
  });

  it("falls back to window.location.href when url not supplied", () => {
    reportClientError({
      event: "client.reported_error",
      message: "anything",
    });
    const body = lastFetchBody(fetchCalls);
    expect(body.url).toBe(window.location.href);
  });

  it("clips message and stack with a marker", () => {
    const longMessage = "m".repeat(2000);
    const longStack = "s".repeat(8000);
    reportClientError({
      event: "client.reported_error",
      message: longMessage,
      stack: longStack,
    });
    const body = lastFetchBody(fetchCalls);
    expect(typeof body.message).toBe("string");
    expect(typeof body.stack).toBe("string");
    expect((body.message as string).endsWith("...[clipped]")).toBe(true);
    expect((body.stack as string).endsWith("...[clipped]")).toBe(true);
    // Stack gets a 4x larger budget than the other fields — without
    // this the backend's full stack-trace path would be unreachable
    // since the body cap caps total bytes.
    expect((body.stack as string).length).toBeGreaterThan(
      (body.message as string).length,
    );
  });

  it("never throws even when fetch rejects", () => {
    globalThis.fetch = vi.fn(() => {
      throw new Error("network down");
    }) as unknown as typeof fetch;
    expect(() =>
      reportClientError({
        event: "client.unhandled_error",
        message: "boom",
      }),
    ).not.toThrow();
  });
});

describe("installClientErrorReporter", () => {
  it("captures window error events and posts client.unhandled_error", () => {
    installClientErrorReporter();
    const err = new Error("kaboom");
    window.dispatchEvent(
      new ErrorEvent("error", {
        message: err.message,
        error: err,
        filename: "/assets/index.js",
        lineno: 7,
        colno: 13,
      }),
    );
    const body = lastFetchBody(fetchCalls);
    expect(body.event).toBe("client.unhandled_error");
    expect(body.message).toBe("kaboom");
    expect(body.source).toBe("/assets/index.js");
    expect(body.line).toBe(7);
    expect(body.column).toBe(13);
  });

  it("captures unhandled rejection events and posts client.unhandled_rejection", () => {
    installClientErrorReporter();
    const reason = new Error("denied");
    // ``PromiseRejectionEvent`` doesn't construct cleanly on jsdom;
    // synthesise a CustomEvent and back-fill the ``reason`` field
    // with Object.defineProperty so the listener's ``event.reason``
    // read works the same way it does in a real browser.
    const event = new Event("unhandledrejection") as Event & {
      reason: unknown;
    };
    Object.defineProperty(event, "reason", { value: reason });
    window.dispatchEvent(event);
    const body = lastFetchBody(fetchCalls);
    expect(body.event).toBe("client.unhandled_rejection");
    expect(body.message).toBe("denied");
  });

  it("is idempotent — installing twice does not double-post", () => {
    installClientErrorReporter();
    installClientErrorReporter();
    window.dispatchEvent(
      new ErrorEvent("error", { message: "once" }),
    );
    expect(fetchCalls).toHaveLength(1);
  });
});
