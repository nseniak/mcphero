/**
 * Component-level coverage for TemplateVarsManager.
 *
 * Buffered mode (``upstreamId=""``) doesn't hit the network — env
 * vars live in component state and are reported back via
 * ``onBufferedChange``. That makes it the right surface for testing
 * the modal validation, the masked vs plain display, the cross-
 * reference badges, the "Define <NAME>" affordance, and the
 * "Treat as password" toggle without mocking fetch.
 *
 * Deferred mode (``pendingChanges`` + ``onPendingChange``) is the
 * detail-page edit-mode buffer. Its server-list fetch is mocked at
 * the API module boundary so we can test the buffer overlay
 * (sets / deletes), the un-delete-on-readd flow, and the Save-flush
 * shape the parent will ship to the backend.
 *
 * The full edit→Save→reload round-trip is covered by Playwright
 * (it requires the real backend round-trip).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  TemplateVarsManager,
  EMPTY_PENDING_CHANGES,
  type BufferedTemplateVar,
  type TemplateVarsManagerHandle,
  type PendingTemplateVarChanges,
} from "./TemplateVarsManager";
import * as envVarsApi from "../api/template-vars";
import * as sandboxFilesApi from "../api/sandbox-files";
import type { TemplateVarSummary } from "../api/types";
import type { TemplateVarReference } from "../lib/template-var-references";

function noRefs(): TemplateVarReference[] {
  return [];
}

function secret(value: string): BufferedTemplateVar {
  return { value, is_secret: true };
}

function plain(value: string): BufferedTemplateVar {
  return { value, is_secret: false };
}

describe("TemplateVarsManager (buffered mode)", () => {
  it("renders the empty state when no env vars are defined", () => {
    render(
      <TemplateVarsManager upstreamId="" references={noRefs()} />,
    );
    expect(
      screen.getByText(/No variables defined/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Add variable/ }),
    ).toBeInTheDocument();
  });

  it("modal validates the env-var name against the regex", async () => {
    const user = userEvent.setup();
    render(
      <TemplateVarsManager upstreamId="" references={noRefs()} />,
    );
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    const nameInput = within(dialog).getByLabelText(/Name/i);
    const valueInput = within(dialog).getByPlaceholderText(/Paste value/);
    const save = within(dialog).getByRole("button", { name: /^Save$/ });
    expect(save).toBeDisabled();
    await user.type(nameInput, "1leading");
    await user.type(valueInput, "anything");
    expect(save).toBeDisabled();
  });

  it("buffered save reports a secret-typed BufferedTemplateVar", async () => {
    const user = userEvent.setup();
    const onBufferedChange = vi.fn();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        onBufferedChange={onBufferedChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "GH_TOKEN");
    await user.type(
      within(dialog).getByPlaceholderText(/Paste value/),
      "ghp_value_more_than_16_chars",
    );
    // "Treat as password" defaults to on — no extra click needed.
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onBufferedChange).toHaveBeenCalledWith({
      GH_TOKEN: { value: "ghp_value_more_than_16_chars", is_secret: true },
    });
    expect(screen.getByText("GH_TOKEN")).toBeInTheDocument();
    expect(screen.getByText("••••hars")).toBeInTheDocument();
    // The "password" pill is rendered for masked rows.
    expect(screen.getAllByText(/password/i).length).toBeGreaterThan(0);
  });

  it("buffered save with toggle off reports a plain BufferedTemplateVar", async () => {
    const user = userEvent.setup();
    const onBufferedChange = vi.fn();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        onBufferedChange={onBufferedChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "LOG_LEVEL");
    await user.type(
      within(dialog).getByPlaceholderText(/Paste value/),
      "debug",
    );
    // Untick the toggle to make it plain.
    await user.click(within(dialog).getByLabelText(/Treat as password/));
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onBufferedChange).toHaveBeenCalledWith({
      LOG_LEVEL: { value: "debug", is_secret: false },
    });
    // Plain row renders the value verbatim, no mask.
    expect(screen.getByText("LOG_LEVEL")).toBeInTheDocument();
    expect(screen.getByText("debug")).toBeInTheDocument();
    expect(screen.queryByText(/••••/)).toBeNull();
  });

  it("short secret values produce a masked display with no last-4 preview", () => {
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ SHORT: secret("hi") }}
      />,
    );
    expect(screen.getByText("SHORT")).toBeInTheDocument();
    expect(screen.getByText("••••")).toBeInTheDocument();
    expect(screen.queryByText(/••••hi/)).toBeNull();
  });

  it('badges referenced env vars and flags unreferenced ones', () => {
    render(
      <TemplateVarsManager
        upstreamId=""
        references={[
          {
            name: "USED",
            location: { mcpId: "", field: "env", jsonKey: "X" },
          },
          {
            name: "USED",
            location: { mcpId: "", field: "env", jsonKey: "Y" },
          },
        ]}
        initialBuffered={{
          USED: secret("v".repeat(20)),
          UNUSED: secret("v".repeat(20)),
        }}
      />,
    );
    expect(screen.getByText(/Referenced 2×/)).toBeInTheDocument();
    expect(screen.getByText(/Unreferenced/)).toBeInTheDocument();
  });

  it("buffered + isStdio fetches system vars and renders HOME with a 'system' badge", async () => {
    // Bug fix: the create wizard (buffered, isStdio=true) used to
    // skip the system-var fetch entirely, so a stdio config that
    // referenced ${HOME} would fire the amber "undefined" callout
    // and HOME would never appear in the list. Now the wizard
    // fetches with transport=stdio and HOME renders alongside
    // user Variables.
    listSystemVariablesSpy.mockResolvedValue([
      { name: "HOME", value: "/home/user" },
    ]);
    render(
      <TemplateVarsManager
        upstreamId=""
        references={[
          {
            name: "HOME",
            location: { mcpId: "", field: "args", jsonKey: "0" },
          },
        ]}
        isStdio
      />,
    );
    expect(await screen.findByText("/home/user")).toBeInTheDocument();
    expect(screen.getByText(/^system$/)).toBeInTheDocument();
    expect(listSystemVariablesSpy).toHaveBeenCalledWith("stdio");
    // ${HOME} reference is now resolved (not flagged as undefined).
    expect(
      screen.queryByText(/references undefined variables/i),
    ).toBeNull();
  });

  it("buffered + http transport receives [] from the system-var endpoint", async () => {
    // The wizard's transport flips when the operator pastes a URL
    // or HTTP-shaped JSON. The backend returns [] for non-stdio,
    // so no system row should render even if the spy is set up.
    listSystemVariablesSpy.mockResolvedValue([]);
    render(
      <TemplateVarsManager upstreamId="" references={noRefs()} />,
    );
    // Wait one microtask cycle for the fetch to resolve, then assert
    // no system row exists. ``findByText`` would block; we want a
    // negative assertion after the loading state clears.
    await screen.findByText(/No variables defined/);
    expect(listSystemVariablesSpy).toHaveBeenCalledWith("streamable_http");
    expect(screen.queryByText(/^system$/)).toBeNull();
  });

  it("renders the unresolved-callout for a ${MISSING} reference in args", async () => {
    // Substitution covers args / command / url too, not just env /
    // headers. The amber callout must fire on a ``${MISSING}`` token
    // anywhere the runtime would substitute, otherwise the operator
    // saves a config that fails at session start with no visible
    // warning.
    const user = userEvent.setup();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={[
          {
            name: "MISSING",
            location: { mcpId: "", field: "args", jsonKey: "0" },
          },
        ]}
      />,
    );
    expect(
      screen.getByText(/references undefined variables/i),
    ).toBeInTheDocument();
    expect(screen.getByText("${MISSING}")).toBeInTheDocument();
    const addBtn = screen.getByRole("button", { name: /^Add$/ });
    await user.click(addBtn);
    const dialog = await screen.findByRole("dialog");
    const nameField = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    expect(nameField.value).toBe("MISSING");
    expect(nameField).toBeDisabled();
  });

  it("openDefine via imperative ref opens Add modal pre-filled and locked", async () => {
    // The unresolved-warning surface lives in JsonReferencesPanel;
    // this test pins the imperative entry point that the panel
    // calls when the user clicks "Define <NAME>".
    const ref: { current: TemplateVarsManagerHandle | null } = { current: null };
    render(
      <TemplateVarsManager
        upstreamId=""
        references={[]}
        handleRef={ref}
      />,
    );
    expect(ref.current).not.toBeNull();
    ref.current!.openDefine("MISSING");
    const dialog = await screen.findByRole("dialog");
    const nameField = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    expect(nameField.value).toBe("MISSING");
    expect(nameField).toBeDisabled();
    expect(within(dialog).getByLabelText(/Treat as password/)).toBeInTheDocument();
  });

  it("Replace modal pre-fills the name (editable) and hides the toggle", async () => {
    const user = userEvent.setup();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ TOKEN: secret("v".repeat(20)) }}
      />,
    );
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    // Title flipped from "Replace" to "Edit" — the modal supports
    // renames now, not just value replacement.
    expect(within(dialog).getByText(/Edit TOKEN/)).toBeInTheDocument();
    const nameInput = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    expect(nameInput.value).toBe("TOKEN");
    // Name is editable on Edit (rename support).
    expect(nameInput).not.toBeDisabled();
    // Toggle is hidden — secrecy is a create-time decision.
    expect(within(dialog).queryByLabelText(/Treat as password/)).toBeNull();
  });

  it("password row obfuscates by default; eye toggle reveals", async () => {
    const user = userEvent.setup();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{
          GH_TOKEN: secret("ghp_supersecretvalue1234"),
        }}
      />,
    );
    // Default render — masked display with last-4 preview, no
    // plaintext on screen.
    expect(screen.getByText("••••1234")).toBeInTheDocument();
    expect(screen.queryByText(/ghp_supersecretvalue1234/)).toBeNull();
    // Click the eye to reveal.
    await user.click(screen.getByLabelText(/Reveal value/i));
    expect(screen.getByText("ghp_supersecretvalue1234")).toBeInTheDocument();
    // Click again to hide.
    await user.click(screen.getByLabelText(/Hide value/i));
    expect(screen.queryByText(/ghp_supersecretvalue1234/)).toBeNull();
    expect(screen.getByText("••••1234")).toBeInTheDocument();
  });

  it("password row copy button copies plaintext without revealing it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{
          GH_TOKEN: secret("ghp_anothertokenvalue9876"),
        }}
      />,
    );
    // Copy without revealing — paste-into-form ergonomics without
    // putting the secret on screen.
    await user.click(screen.getByLabelText(/Copy value/i));
    expect(writeText).toHaveBeenCalledWith("ghp_anothertokenvalue9876");
    // Plaintext still not on screen.
    expect(screen.queryByText("ghp_anothertokenvalue9876")).toBeNull();
  });

  it("Edit modal renames a buffered variable (delete-old + set-new)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ OLD_NAME: secret("buffered-value-1234567890") }}
        onBufferedChange={onChange}
      />,
    );
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    const name = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    expect(name.value).toBe("OLD_NAME");
    await user.clear(name);
    await user.type(name, "NEW_NAME");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    // Buffer should now hold NEW_NAME, not OLD_NAME.
    expect(onChange).toHaveBeenCalledWith({
      NEW_NAME: { value: "buffered-value-1234567890", is_secret: true },
    });
    expect(screen.getByText("NEW_NAME")).toBeInTheDocument();
    expect(screen.queryByText("OLD_NAME")).toBeNull();
  });

  it("Edit modal blocks rename to a name that already exists", async () => {
    const user = userEvent.setup();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{
          ORIGINAL: secret("v".repeat(20)),
          OTHER: secret("w".repeat(20)),
        }}
      />,
    );
    await user.click(screen.getAllByTitle(/Replace value/)[0]);
    const dialog = screen.getByRole("dialog");
    const name = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    await user.clear(name);
    await user.type(name, "OTHER");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(within(dialog).getByText(/already exists/)).toBeInTheDocument();
  });

  it("plain-row copy button writes the full value to the clipboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{
          LONG: plain(
            "this-is-a-very-long-plain-value-that-exceeds-the-truncate-threshold-clearly",
          ),
        }}
      />,
    );
    await user.click(screen.getByLabelText(/Copy value/i));
    expect(writeText).toHaveBeenCalledWith(
      "this-is-a-very-long-plain-value-that-exceeds-the-truncate-threshold-clearly",
    );
    // Briefly flips to the "Copied" affordance.
    expect(screen.getByLabelText(/Copied/i)).toBeInTheDocument();
  });

  it("plain-row Replace shows value as text (no password masking)", async () => {
    const user = userEvent.setup();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ LOG_LEVEL: plain("debug") }}
      />,
    );
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    const valueInput = within(dialog).getByPlaceholderText(
      /Paste value/,
    ) as HTMLInputElement;
    expect(valueInput.type).toBe("text");
    // No "value can't be viewed again" warning.
    expect(
      within(dialog).queryByText(/can't be viewed again/),
    ).toBeNull();
  });

  it("blocks Add when the name already exists in the buffer", async () => {
    const user = userEvent.setup();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ TOKEN: secret("existing-value-1234567890") }}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "TOKEN");
    await user.type(
      within(dialog).getByPlaceholderText(/Paste value/),
      "another-value-1234567890",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    // The save is blocked with an inline error; the dialog stays open.
    expect(
      within(dialog).getByText(/already exists/i),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/Replace it from the list/i)).toBeInTheDocument();
  });
});

// --- Deferred mode (detail-page edit) ---

const listTemplateVarsSpy = vi.spyOn(envVarsApi, "listTemplateVars");
// The component's deferred-mode refresh fetches three endpoints in
// parallel; without these spies the unmocked listSystemVariables /
// listSandboxFiles calls hit ``fetch`` and reject in jsdom, which
// poisons the whole Promise.all and leaves the rendered UI showing
// "No variables defined." regardless of what listTemplateVars returns.
const listSystemVariablesSpy = vi.spyOn(sandboxFilesApi, "listSystemVariables");
const listSandboxFilesSpy = vi.spyOn(sandboxFilesApi, "listSandboxFiles");

beforeEach(() => {
  listTemplateVarsSpy.mockReset();
  listSystemVariablesSpy.mockReset().mockResolvedValue([]);
  listSandboxFilesSpy.mockReset().mockResolvedValue([]);
});

afterEach(() => {
  listTemplateVarsSpy.mockReset();
  listSystemVariablesSpy.mockReset();
  listSandboxFilesSpy.mockReset();
});

function summary(
  name: string,
  opts: { is_secret?: boolean; value?: string | null; last_four?: string | null } = {},
): TemplateVarSummary {
  // Default to a non-null value when one isn't provided so the row
  // renders the cell (obfuscated for secret rows, verbatim for plain
  // rows). The "(no value)" fallback is back-compat only.
  const explicit = "value" in opts;
  const last_four = opts.last_four ?? null;
  const inferredValue =
    last_four !== null
      ? `placeholder-${last_four}`
      : "placeholder-value";
  return {
    name,
    is_secret: opts.is_secret ?? true,
    value: explicit ? (opts.value ?? null) : inferredValue,
    last_four,
    created_at: new Date(0).toISOString(),
    updated_at: new Date(0).toISOString(),
  };
}

describe("TemplateVarsManager (deferred mode)", () => {
  it("renders the server list, then layers a buffered Add on top", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("EXISTING_TOKEN", { is_secret: true, last_four: "abcd" }),
    ]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = EMPTY_PENDING_CHANGES;
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    const { rerender } = render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    // Server row appears once the fetch resolves.
    expect(await screen.findByText("EXISTING_TOKEN")).toBeInTheDocument();
    expect(screen.getByText(/••••abcd/)).toBeInTheDocument();
    // Add a new row via the modal — should land in pending, not the API.
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "NEW_TOKEN");
    await user.type(
      within(dialog).getByPlaceholderText(/Paste value/),
      "fresh-value-1234567890",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: { NEW_TOKEN: { value: "fresh-value-1234567890", is_secret: true } },
      deletes: [],
    });
    // Re-render with the parent's new pending state to mimic the
    // controlled-from-parent flow; the new row joins the list.
    rerender(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    expect(await screen.findByText("NEW_TOKEN")).toBeInTheDocument();
    expect(screen.getByText("EXISTING_TOKEN")).toBeInTheDocument();
  });

  it("Rename of a server row queues delete-old + set-new in pending", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("OLD_NAME", {
        is_secret: true,
        value: "server-value-1234567890",
        last_four: "7890",
      }),
    ]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = EMPTY_PENDING_CHANGES;
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    expect(await screen.findByText("OLD_NAME")).toBeInTheDocument();
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    const name = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    await user.clear(name);
    await user.type(name, "NEW_NAME");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    // Server-row rename: queue OLD_NAME for delete (so the flush
    // wipes the old row) AND set NEW_NAME with the same value.
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: {
        NEW_NAME: { value: "server-value-1234567890", is_secret: true },
      },
      deletes: ["OLD_NAME"],
    });
  });

  it("Delete on a server row queues a delete entry in pending", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("DROP_ME", { is_secret: false, value: "v" }),
    ]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = EMPTY_PENDING_CHANGES;
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    expect(await screen.findByText("DROP_ME")).toBeInTheDocument();
    await user.click(screen.getByTitle(/Delete variable/));
    // No confirm dialog — DROP_ME is unreferenced, so the delete
    // applies immediately. (References would trigger the louder
    // "running config will fail" prompt.)
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: {},
      deletes: ["DROP_ME"],
    });
  });

  it("Add on a name that's queued for delete moves it back into sets (un-delete)", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("FLIPFLOP", { is_secret: true, last_four: "1234" }),
    ]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = { sets: {}, deletes: ["FLIPFLOP"] };
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    const { rerender } = render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    // FLIPFLOP is queued for delete → not visible.
    await screen.findByRole("button", { name: /Add variable/ });
    expect(screen.queryByText("FLIPFLOP")).toBeNull();
    // User adds FLIPFLOP back with a new value.
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "FLIPFLOP");
    await user.type(
      within(dialog).getByPlaceholderText(/Paste value/),
      "back-again-1234567890",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: { FLIPFLOP: { value: "back-again-1234567890", is_secret: true } },
      deletes: [],
    });
    rerender(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    expect(await screen.findByText("FLIPFLOP")).toBeInTheDocument();
  });

  it("blocks Add when the name already exists on the server", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("SERVER_TOKEN", { is_secret: true, last_four: "abcd" }),
    ]);
    const user = userEvent.setup();
    const onPendingChange = vi.fn();
    render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={EMPTY_PENDING_CHANGES}
        onPendingChange={onPendingChange}
      />,
    );
    await screen.findByText("SERVER_TOKEN");
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "SERVER_TOKEN");
    await user.type(
      within(dialog).getByPlaceholderText(/Paste value/),
      "another-value-1234567890",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(within(dialog).getByText(/already exists/i)).toBeInTheDocument();
    expect(onPendingChange).not.toHaveBeenCalled();
  });

  it("Delete on a buffered-only Add drops it from sets without queuing a server delete", async () => {
    listTemplateVarsSpy.mockResolvedValue([]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = {
      sets: { LOCAL_ONLY: { value: "v".repeat(20), is_secret: true } },
      deletes: [],
    };
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    expect(await screen.findByText("LOCAL_ONLY")).toBeInTheDocument();
    await user.click(screen.getByTitle(/Delete variable/));
    // Unreferenced → no confirm dialog; the buffered Add drops out
    // of ``sets`` immediately, no server delete queued.
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: {},
      deletes: [],
    });
  });

  it("Delete on a referenced variable still asks for confirmation", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("BREAK_ME", {
        is_secret: false,
        value: "v",
      }),
    ]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = EMPTY_PENDING_CHANGES;
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={[{
          name: "BREAK_ME",
          location: { mcpId: "", field: "env", jsonKey: "X" },
        }]}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
    expect(await screen.findByText("BREAK_ME")).toBeInTheDocument();
    await user.click(screen.getByTitle(/Delete variable/));
    // Referenced → louder confirm dialog. The variable is not
    // removed until the user clicks Delete in the dialog.
    const confirmDialog = screen.getByRole("dialog");
    await user.click(
      within(confirmDialog).getByRole("button", { name: /^Delete$/ }),
    );
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: {},
      deletes: ["BREAK_ME"],
    });
  });
});
