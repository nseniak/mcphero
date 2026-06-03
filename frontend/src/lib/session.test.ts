import { afterEach, describe, expect, it, vi } from "vitest";

import { notifySessionExpired, setSessionExpiredHandler } from "./session";

afterEach(() => {
  setSessionExpiredHandler(null);
});

describe("session-expiry signal", () => {
  it("invokes the registered handler on notify", () => {
    const handler = vi.fn();
    setSessionExpiredHandler(handler);
    notifySessionExpired();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("is a no-op when no handler is registered", () => {
    expect(() => notifySessionExpired()).not.toThrow();
  });

  it("stops invoking after the handler is cleared", () => {
    const handler = vi.fn();
    setSessionExpiredHandler(handler);
    setSessionExpiredHandler(null);
    notifySessionExpired();
    expect(handler).not.toHaveBeenCalled();
  });
});
