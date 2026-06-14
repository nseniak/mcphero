import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SandboxCapabilitiesResponse } from "../api/types";
import { SandboxComboSelect } from "./SandboxComboSelect";

function localSubprocessCaps(): SandboxCapabilitiesResponse {
  // Mirrors LocalSubprocessSandboxService.capabilities(): disk axis is a
  // flat [0] and resources are not enforced.
  return {
    provider: "local-subprocess",
    allowed_cpu_vcpus: [1, 2],
    allowed_memory_mb: [1024, 2048],
    allowed_disk_gb: [0],
    allowed_combinations: [
      { cpu_vcpus: 1, memory_mb: 1024, disk_gb: 0 },
      { cpu_vcpus: 2, memory_mb: 2048, disk_gb: 0 },
    ],
    enforces_resources: false,
    supports_pause_resume: false,
    supports_egress_filtering: false,
    supports_persistent_disk: false,
  };
}

function e2bCaps(): SandboxCapabilitiesResponse {
  return {
    provider: "e2b",
    allowed_cpu_vcpus: [1, 2],
    allowed_memory_mb: [2048, 4096],
    allowed_disk_gb: [],
    allowed_combinations: [
      { cpu_vcpus: 1, memory_mb: 2048, disk_gb: 0 },
      { cpu_vcpus: 2, memory_mb: 4096, disk_gb: 0 },
    ],
    enforces_resources: true,
    supports_pause_resume: true,
    supports_egress_filtering: false,
    supports_persistent_disk: false,
  };
}

describe("SandboxComboSelect", () => {
  it("disables the picker and explains when resources are not enforced", () => {
    render(
      <SandboxComboSelect
        capabilities={localSubprocessCaps()}
        value={{ cpu_vcpus: 1, memory_mb: 1024, disk_gb: 0 }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByText(/not enforced in local mode/i)).toBeTruthy();
  });

  it("never renders a '0 GiB disk' label", () => {
    render(
      <SandboxComboSelect
        capabilities={localSubprocessCaps()}
        value={{ cpu_vcpus: 1, memory_mb: 1024, disk_gb: 0 }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.queryByText(/0 GiB disk/)).toBeNull();
    // The CPU/RAM part still renders.
    expect(screen.getByRole("option", { name: /1 vCPU \/ 1 GiB/ })).toBeTruthy();
  });

  it("enables the picker when the provider enforces resources", () => {
    render(
      <SandboxComboSelect
        capabilities={e2bCaps()}
        value={{ cpu_vcpus: 1, memory_mb: 2048, disk_gb: 0 }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox")).not.toBeDisabled();
    expect(screen.queryByText(/not enforced in local mode/i)).toBeNull();
  });
});
