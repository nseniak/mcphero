/**
 * Component-level coverage for SandboxFilesManager — focused on the
 * in-flight ``${VAR}`` detection inside the Add/Replace modal's
 * target_path input. The parent's amber callout only re-scans after
 * the modal closes and the file list refetches; this card surfaces
 * unknown variables live, right next to the field, so a typo doesn't
 * silently survive Save.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SandboxFilesManager } from "./SandboxFilesManager";
import * as sandboxFilesApi from "../api/sandbox-files";
import * as templateVarsApi from "../api/template-vars";

function makeSystemVar(name: string, value: string) {
  return { name, value };
}

function makeUserVar(name: string, value: string | null) {
  return {
    name,
    is_secret: value === null,
    value,
    last_four: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function mockApis(opts: {
  systemVars?: ReturnType<typeof makeSystemVar>[];
  userVars?: ReturnType<typeof makeUserVar>[];
}) {
  vi.spyOn(sandboxFilesApi, "listSandboxFiles").mockResolvedValue([]);
  vi.spyOn(sandboxFilesApi, "listSystemVariables").mockResolvedValue(
    opts.systemVars ?? [],
  );
  vi.spyOn(templateVarsApi, "listTemplateVars").mockResolvedValue(
    opts.userVars ?? [],
  );
}

describe("SandboxFilesManager — modal target_path live unknown-var detection", () => {
  beforeEach(() => {
    mockApis({
      systemVars: [makeSystemVar("HOME", "/home/runner")],
      userVars: [makeUserVar("MY_VAR", "value")],
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("flags an unknown ${VAR} as the operator types it", async () => {
    const user = userEvent.setup();
    render(<SandboxFilesManager upstreamId="up_test" />);
    await user.click(await screen.findByRole("button", { name: /Add file/ }));
    const targetInput = await screen.findByPlaceholderText(
      /\$\{HOME\}\/.config\/gcloud\/credentials.json/,
    );
    await user.type(targetInput, "${{UNKNOWN_VAR}/x");
    expect(screen.getByText(/Unknown variable/)).toBeInTheDocument();
    expect(screen.getByText("${UNKNOWN_VAR}")).toBeInTheDocument();
  });

  it("does not flag defined system or user variables", async () => {
    const user = userEvent.setup();
    render(<SandboxFilesManager upstreamId="up_test" />);
    await user.click(await screen.findByRole("button", { name: /Add file/ }));
    const targetInput = await screen.findByPlaceholderText(
      /\$\{HOME\}\/.config\/gcloud\/credentials.json/,
    );
    await user.type(targetInput, "${{HOME}/${{MY_VAR}/file");
    expect(screen.queryByText(/Unknown variable/)).toBeNull();
  });

  it("clears the warning once the operator fixes the typo", async () => {
    const user = userEvent.setup();
    render(<SandboxFilesManager upstreamId="up_test" />);
    await user.click(await screen.findByRole("button", { name: /Add file/ }));
    const targetInput = await screen.findByPlaceholderText(
      /\$\{HOME\}\/.config\/gcloud\/credentials.json/,
    );
    await user.type(targetInput, "${{HMOE}/x");
    expect(screen.getByText(/Unknown variable/)).toBeInTheDocument();
    await user.clear(targetInput);
    await user.type(targetInput, "${{HOME}/x");
    expect(screen.queryByText(/Unknown variable/)).toBeNull();
  });

  it("pluralises and lists each distinct unknown variable once", async () => {
    const user = userEvent.setup();
    render(<SandboxFilesManager upstreamId="up_test" />);
    await user.click(await screen.findByRole("button", { name: /Add file/ }));
    const targetInput = await screen.findByPlaceholderText(
      /\$\{HOME\}\/.config\/gcloud\/credentials.json/,
    );
    await user.type(targetInput, "${{A}/${{B}/${{A}");
    expect(screen.getByText(/Unknown variables/)).toBeInTheDocument();
    expect(screen.getByText("${A}")).toBeInTheDocument();
    expect(screen.getByText("${B}")).toBeInTheDocument();
  });
});
