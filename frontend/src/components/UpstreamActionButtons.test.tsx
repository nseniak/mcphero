import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const signOutUpstream = vi.fn();

vi.mock("../api/admin", () => ({
  connectUpstream: vi.fn(),
  disconnectUpstream: vi.fn(),
  reconnectUpstream: vi.fn(),
  signOutUpstream: (id: string, email: string) => signOutUpstream(id, email),
}));
vi.mock("../hooks/useEventSource", () => ({
  useEventSource: () => ({ connected: false }),
}));

import { UpstreamActionButtons } from "./UpstreamActionButtons";

afterEach(() => {
  signOutUpstream.mockReset();
});

function renderButtons(props: {
  ready: boolean;
  authMode: string;
  slotOwner?: string | null;
  stopped?: boolean;
  reload?: () => void;
}) {
  return render(
    <UpstreamActionButtons
      id="notion"
      transport="streamable_http"
      ready={props.ready}
      authMode={props.authMode}
      slotOwner={props.slotOwner ?? null}
      stopped={props.stopped ?? false}
      reload={props.reload ?? (() => {})}
    />,
  );
}

describe("UpstreamActionButtons Remove sign-in", () => {
  it("offers Remove sign-in next to Disconnect while an admin's sign-in serves the MCP", () => {
    renderButtons({ ready: true, authMode: "admin_oauth", slotOwner: "alice@co.com" });
    expect(screen.getByRole("button", { name: /Disconnect/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Remove sign-in/ })).toBeVisible();
  });

  it("offers Connect and Remove sign-in on a stopped MCP whose sign-in was kept", () => {
    renderButtons({
      ready: false, authMode: "per_user_oauth", stopped: true, slotOwner: "alice@co.com",
    });
    expect(screen.getByRole("button", { name: /^Connect$/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Remove sign-in/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Disconnect/ })).toBeNull();
  });

  it("offers only Authenticate on a stopped MCP with no sign-in kept", () => {
    renderButtons({ ready: false, authMode: "admin_oauth", stopped: true });
    expect(screen.getByRole("button", { name: /Authenticate/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Remove sign-in/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Disconnect/ })).toBeNull();
  });

  it("lets an admin stop a running MCP that has no admin sign-in", () => {
    renderButtons({ ready: false, authMode: "per_user_oauth", stopped: false });
    expect(screen.getByRole("button", { name: /Authenticate/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Disconnect/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Remove sign-in/ })).toBeNull();
  });

  it("has no Remove sign-in on an MCP without sign-in", () => {
    renderButtons({ ready: true, authMode: "service_account" });
    expect(screen.queryByRole("button", { name: /Remove sign-in/ })).toBeNull();
  });

  it("asks first, naming the admin, then removes that admin's sign-in and reloads", async () => {
    signOutUpstream.mockResolvedValue({ status: "signed_out", email: "alice@co.com" });
    const reload = vi.fn();
    renderButtons({
      ready: true, authMode: "admin_oauth", slotOwner: "alice@co.com", reload,
    });

    fireEvent.click(screen.getByRole("button", { name: /Remove sign-in/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("alice@co.com");
    expect(signOutUpstream).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole("button", { name: /Remove sign-in/ }).at(-1)!);

    await waitFor(() =>
      expect(signOutUpstream).toHaveBeenCalledWith("notion", "alice@co.com"),
    );
    await waitFor(() => expect(reload).toHaveBeenCalled());
  });

  it("shows why when the backend refuses", async () => {
    signOutUpstream.mockRejectedValue(
      new Error("The sign-in shown is now bob@co.com's, not alice@co.com's."),
    );
    renderButtons({ ready: true, authMode: "admin_oauth", slotOwner: "alice@co.com" });

    fireEvent.click(screen.getByRole("button", { name: /Remove sign-in/ }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getAllByRole("button", { name: /Remove sign-in/ }).at(-1)!);

    await waitFor(() =>
      expect(screen.getByRole("dialog")).toHaveTextContent("now bob@co.com's"),
    );
  });

  it("does nothing when the admin cancels", async () => {
    renderButtons({ ready: true, authMode: "admin_oauth", slotOwner: "alice@co.com" });

    fireEvent.click(screen.getByRole("button", { name: /Remove sign-in/ }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: /Cancel/ }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(signOutUpstream).not.toHaveBeenCalled();
  });
});
