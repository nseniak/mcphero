import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthProvider } from "./useAuth";
import { notifySessionExpired, setSessionExpiredHandler } from "../lib/session";
import { ApiError } from "../api/client";
import type { UserInfo } from "../api/types";

vi.mock("../api/auth", () => ({
  fetchMe: vi.fn(),
  fetchAuthStatus: vi.fn(),
  logout: vi.fn(),
}));
vi.mock("../lib/sentry", () => ({ setSentryUser: vi.fn() }));
vi.mock("../lib/analytics", () => ({ identify: vi.fn(), reset: vi.fn() }));

import { fetchMe, fetchAuthStatus } from "../api/auth";

function makeUser(): UserInfo {
  return {
    email: "alice@example.com",
    roles: ["admin"],
    is_admin: true,
    is_superadmin: false,
    orgs: [],
    current_org: null,
  };
}

beforeEach(() => {
  vi.mocked(fetchAuthStatus).mockResolvedValue({ has_users: true });
});

afterEach(() => {
  setSessionExpiredHandler(null);
  vi.clearAllMocks();
});

describe("useAuthProvider mid-session expiry", () => {
  it("flips to the session-expired error once a live session 401s", async () => {
    vi.mocked(fetchMe).mockResolvedValue(makeUser());
    const { result } = renderHook(() => useAuthProvider());

    await waitFor(() => expect(result.current.user).not.toBeNull());

    act(() => {
      notifySessionExpired();
    });

    expect(result.current.user).toBeNull();
    expect(result.current.error).toMatch(/session expired/i);
  });

  it("ignores a 401 seen before the user ever authenticated", async () => {
    // Bootstrap 401: not signed in yet. The handler must stay quiet so
    // the app shows the normal sign-in screen, not "Session Expired".
    vi.mocked(fetchMe).mockRejectedValue(new ApiError(401, "Not authenticated"));
    const { result } = renderHook(() => useAuthProvider());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.user).toBeNull();

    act(() => {
      notifySessionExpired();
    });

    expect(result.current.user).toBeNull();
    expect(result.current.error).toBeNull();
  });
});
