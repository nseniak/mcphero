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
    expect(screen.getByText("•••• set")).toBeInTheDocument();
    expect(screen.queryByText(/ghp_value_more_than_16_chars/)).toBeNull();
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

  it("password rows show only set or empty, never any part of the value", () => {
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ SHORT: secret("hi"), BLANK: secret("") }}
      />,
    );
    expect(screen.getByText("•••• set")).toBeInTheDocument();
    expect(screen.getByText("empty")).toBeInTheDocument();
    expect(screen.queryByText(/hi$/)).toBeNull();
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

  it("password row has no reveal or copy control", () => {
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{
          GH_TOKEN: secret("ghp_supersecretvalue1234"),
        }}
      />,
    );
    expect(screen.getByText("•••• set")).toBeInTheDocument();
    expect(screen.queryByText(/ghp_supersecretvalue1234/)).toBeNull();
    expect(screen.queryByText(/1234/)).toBeNull();
    expect(screen.queryByLabelText(/Reveal value/i)).toBeNull();
    expect(screen.queryByLabelText(/Copy value/i)).toBeNull();
  });

  it("password Edit starts blank and blank keeps the buffered value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TemplateVarsManager
        upstreamId=""
        references={noRefs()}
        initialBuffered={{ TOKEN: secret("buffered-value-1234567890") }}
        onBufferedChange={onChange}
      />,
    );
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    const value = within(dialog).getByPlaceholderText(
      /Leave blank to keep the saved value/,
    ) as HTMLInputElement;
    expect(value.value).toBe("");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onChange).toHaveBeenCalledWith({
      TOKEN: { value: "buffered-value-1234567890", is_secret: true },
    });
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

/** A server row as the API sends it: a password never carries its
 *  value, a plain row does. */
function summary(
  name: string,
  opts: { is_secret?: boolean; value?: string; has_value?: boolean } = {},
): TemplateVarSummary {
  const isSecret = opts.is_secret ?? true;
  const value = opts.value ?? "placeholder-value";
  return {
    name,
    is_secret: isSecret,
    value: isSecret ? null : value,
    has_value: opts.has_value ?? true,
    created_at: new Date(0).toISOString(),
    updated_at: new Date(0).toISOString(),
  };
}

function renderDeferred(pending: PendingTemplateVarChanges, onPendingChange: (next: PendingTemplateVarChanges) => void) {
  return render(
    <TemplateVarsManager
      upstreamId="srv-id"
      references={noRefs()}
      pendingChanges={pending}
      onPendingChange={onPendingChange}
    />,
  );
}

describe("TemplateVarsManager (deferred mode, write-only passwords)", () => {
  it("Edit of a server password with a blank value queues nothing", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("TOKEN")]);
    const user = userEvent.setup();
    const onPendingChange = vi.fn();
    renderDeferred(EMPTY_PENDING_CHANGES, onPendingChange);
    await screen.findByText("TOKEN");
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onPendingChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Edit of a server password with a typed value queues the new value", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("TOKEN")]);
    const user = userEvent.setup();
    const onPendingChange = vi.fn();
    renderDeferred(EMPTY_PENDING_CHANGES, onPendingChange);
    await screen.findByText("TOKEN");
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    await user.type(
      within(dialog).getByPlaceholderText(/Leave blank to keep the saved value/),
      "new-password",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: { TOKEN: { value: "new-password", is_secret: true } },
      deletes: [],
    });
  });

  it("Clear queues an empty value and the row then reads empty", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("TOKEN")]);
    const user = userEvent.setup();
    let pending: PendingTemplateVarChanges = EMPTY_PENDING_CHANGES;
    const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
      pending = next;
    });
    const { rerender } = renderDeferred(pending, onPendingChange);
    await screen.findByText("TOKEN");
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByLabelText(/Clear the saved value/));
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: { TOKEN: { value: "", is_secret: true } },
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
    expect(screen.getByText("empty")).toBeInTheDocument();
  });

  it("a kept-value rename shows the row under its new name as set", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("OLD_NAME")]);
    renderDeferred(
      {
        sets: { NEW_NAME: { value: null, is_secret: true, rename_from: "OLD_NAME" } },
        deletes: ["OLD_NAME"],
      },
      vi.fn(),
    );
    expect(await screen.findByText("NEW_NAME")).toBeInTheDocument();
    expect(screen.queryByText("OLD_NAME")).toBeNull();
    expect(screen.getByText("•••• set")).toBeInTheDocument();
  });

  it("renaming twice keeps pointing at the original saved row", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("FIRST")]);
    const user = userEvent.setup();
    const onPendingChange = vi.fn();
    renderDeferred(
      {
        sets: { SECOND: { value: null, is_secret: true, rename_from: "FIRST" } },
        deletes: ["FIRST"],
      },
      onPendingChange,
    );
    await screen.findByText("SECOND");
    await user.click(screen.getByTitle(/Replace value/));
    const dialog = screen.getByRole("dialog");
    const name = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
    await user.clear(name);
    await user.type(name, "THIRD");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: { THIRD: { value: null, is_secret: true, rename_from: "FIRST" } },
      deletes: ["FIRST"],
    });
  });
});

function plainSummary(name: string, value: string): TemplateVarSummary {
  return summary(name, { is_secret: false, value, has_value: value !== "" });
}

/** Render in deferred mode and keep the parent's pending state, the
 *  way the detail page does. */
function renderWithLivePending(initial: PendingTemplateVarChanges = EMPTY_PENDING_CHANGES) {
  let pending = initial;
  const onPendingChange = vi.fn((next: PendingTemplateVarChanges) => {
    pending = next;
  });
  const view = render(
    <TemplateVarsManager
      upstreamId="srv-id"
      references={noRefs()}
      pendingChanges={pending}
      onPendingChange={onPendingChange}
    />,
  );
  const sync = () =>
    view.rerender(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={noRefs()}
        pendingChanges={pending}
        onPendingChange={onPendingChange}
      />,
    );
  return { getPending: () => pending, sync };
}

async function clickInRow(name: string, title: RegExp) {
  const user = userEvent.setup();
  const row = (await screen.findByText(name)).closest("li");
  if (!row) throw new Error(`no row ${name}`);
  await user.click(within(row as HTMLElement).getByTitle(title));
  return user;
}

async function renameInModal(user: ReturnType<typeof userEvent.setup>, newName: string) {
  const dialog = screen.getByRole("dialog");
  const name = within(dialog).getByLabelText(/Name/i) as HTMLInputElement;
  await user.clear(name);
  await user.type(name, newName);
  await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
}

describe("TemplateVarsManager (deferred mode, reused names)", () => {
  it("delete X, then rename password P onto X: X stays in deletes", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("P"), plainSummary("X", "visible")]);
    const { getPending, sync } = renderWithLivePending();
    await clickInRow("X", /Delete variable/);
    sync();
    const user = await clickInRow("P", /Replace value/);
    await renameInModal(user, "X");
    expect(getPending()).toEqual({
      sets: { X: { value: null, is_secret: true, rename_from: "P" } },
      deletes: ["X", "P"],
    });
  });

  it("delete plain X, re-add X as a password: the typed value is never shown", async () => {
    listTemplateVarsSpy.mockResolvedValue([plainSummary("X", "visible")]);
    const { getPending, sync } = renderWithLivePending();
    await clickInRow("X", /Delete variable/);
    sync();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Add variable/ }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Name/i), "X");
    await user.type(within(dialog).getByPlaceholderText(/Paste value/), "typed-new-password-123");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    sync();
    expect(getPending().deletes).toEqual(["X"]);
    expect(await screen.findByText("X")).toBeInTheDocument();
    expect(screen.getByText("•••• set")).toBeInTheDocument();
    expect(screen.queryByText("typed-new-password-123")).toBeNull();
  });

  it("rename P to Q and back to P queues nothing", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("P")]);
    const { getPending, sync } = renderWithLivePending();
    await renameInModal(await clickInRow("P", /Replace value/), "Q");
    sync();
    await renameInModal(await clickInRow("Q", /Replace value/), "P");
    expect(getPending()).toEqual({ sets: {}, deletes: [] });
  });

  it("a kept rename whose source is gone still shows its row", async () => {
    listTemplateVarsSpy.mockResolvedValue([summary("OTHER")]);
    renderWithLivePending({
      sets: { RENAMED: { value: null, is_secret: true, rename_from: "GONE" } },
      deletes: ["GONE"],
    });
    await screen.findByText("OTHER");
    expect(screen.getByText("RENAMED")).toBeInTheDocument();
    expect(screen.getByText("empty")).toBeInTheDocument();
  });
});

describe("TemplateVarsManager (deferred mode)", () => {
  it("renders the server list, then layers a buffered Add on top", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("EXISTING_TOKEN", { is_secret: true }),
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
    expect(screen.getByText("•••• set")).toBeInTheDocument();
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
      summary("OLD_NAME", { is_secret: true }),
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
    // wipes the old row) AND set NEW_NAME, keeping the saved value
    // the dashboard never saw (the backend copies it).
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: {
        NEW_NAME: { value: null, is_secret: true, rename_from: "OLD_NAME" },
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

  it("Add on a name that's queued for delete keeps the delete so the row is recreated", async () => {
    listTemplateVarsSpy.mockResolvedValue([
      summary("FLIPFLOP", { is_secret: true }),
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
    // FLIPFLOP stays in deletes: the save deletes the old row and
    // creates a new one, so the new row takes this flag (a replace
    // in place would keep the old row's flag).
    expect(onPendingChange).toHaveBeenCalledWith({
      sets: { FLIPFLOP: { value: "back-again-1234567890", is_secret: true } },
      deletes: ["FLIPFLOP"],
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
      summary("SERVER_TOKEN", { is_secret: true }),
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

describe("TemplateVarsManager (no edit buffer)", () => {
  it("offers no edit buttons when an MCP id comes without a pending-changes buffer", async () => {
    // With an ``upstreamId`` but neither ``pendingChanges`` nor
    // ``readOnly``, Save used to close the modal and store nothing.
    listTemplateVarsSpy.mockResolvedValue([
      summary("EXISTING_TOKEN", { is_secret: true, has_value: true }),
    ]);
    render(
      <TemplateVarsManager
        upstreamId="srv-id"
        references={[{
          name: "MISSING",
          location: { mcpId: "", field: "env", jsonKey: "X" },
        }]}
      />,
    );
    expect(await screen.findByText("EXISTING_TOKEN")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add variable/ })).toBeNull();
    expect(screen.queryByTitle(/Replace value/)).toBeNull();
    expect(screen.queryByTitle(/Delete variable/)).toBeNull();
    expect(screen.queryByText(/references undefined variables/)).toBeNull();
  });
});
