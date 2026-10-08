import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type EventHandler = (data: unknown) => void;

// The dashboard's live event stream, replaced by the handlers the hook
// registers so a test can deliver an event itself.
const stream: { handlers: Record<string, EventHandler> } = { handlers: {} };

vi.mock("./useEventSource", () => ({
  useEventSource: (handlers: Record<string, EventHandler>) => {
    stream.handlers = handlers;
    return { connected: true };
  },
}));

vi.mock("../api/admin", () => ({
  connectUpstream: vi.fn(),
  disconnectUpstream: vi.fn(),
  reconnectUpstream: vi.fn(),
}));

import { reconnectUpstream } from "../api/admin";
import { useUpstreamActions } from "./useUpstreamActions";

function tokensArrived(upstreamId: string): void {
  act(() => {
    stream.handlers.upstream_tokens_acquired({
      payload: { upstream_id: upstreamId },
    });
  });
}

describe("useUpstreamActions Start on an OAuth upstream", () => {
  beforeEach(() => {
    vi.mocked(reconnectUpstream)
      .mockResolvedValueOnce({
        connected: false,
        authorization_url: "https://idp.example/authorize?state=s",
        error: null,
      })
      .mockResolvedValueOnce({
        connected: true, authorization_url: null, error: null,
      });
    vi.spyOn(window, "open").mockReturnValue({
      closed: false,
      close: vi.fn(),
      location: { href: "" },
    } as unknown as Window);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.mocked(reconnectUpstream).mockReset();
  });

  it("finishes the sign-in as soon as the tokens arrive", async () => {
    const reload = vi.fn();
    const { result } = renderHook(() =>
      useUpstreamActions({ id: "notion", reload }),
    );

    await act(() => result.current.handleReconnect());
    expect(window.open).toHaveBeenCalledTimes(1);
    tokensArrived("notion");

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(reconnectUpstream).toHaveBeenCalledTimes(2);
    expect(result.current.busyAction).toBeNull();
  });

  it("ignores the tokens of another upstream", async () => {
    const reload = vi.fn();
    const { result } = renderHook(() =>
      useUpstreamActions({ id: "notion", reload }),
    );

    await act(() => result.current.handleReconnect());
    tokensArrived("linear");

    expect(reconnectUpstream).toHaveBeenCalledTimes(1);
    expect(result.current.busyAction).toBe("reconnect");
  });
});
