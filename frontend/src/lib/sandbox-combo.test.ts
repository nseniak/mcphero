/**
 * Unit tests for ``firstEnabledCombo`` — the add-upstream wizard's
 * default-combo selector. Guards the regression where the wizard
 * seeded its form off the unfiltered per-axis lists and could land on
 * (or let the user assemble) an off-plan combo the backend gate
 * rejected on submit with a 402. The helper must always return a
 * plan-enabled combo when one exists.
 */
import { describe, expect, it } from "vitest";

import type {
  SandboxCapabilitiesResponse,
  SandboxResourceCombo,
} from "../api/types";
import { firstEnabledCombo, isDockerCommand } from "./sandbox-combo";

function makeCaps(
  combos: SandboxResourceCombo[],
): SandboxCapabilitiesResponse {
  return {
    provider: "e2b",
    // Per-axis lists span the whole grid and are intentionally NOT
    // plan-filtered — the point of the helper is to ignore them and
    // read ``enabled`` off the combos instead.
    allowed_cpu_vcpus: [1, 2, 4, 8],
    allowed_memory_mb: [1024, 2048, 4096, 8192],
    allowed_disk_gb: [],
    allowed_combinations: combos,
    enforces_resources: true,
    supports_pause_resume: true,
    supports_egress_filtering: false,
    supports_persistent_disk: false,
  };
}

function combo(
  cpu_vcpus: number,
  memory_mb: number,
  enabled?: boolean,
): SandboxResourceCombo {
  return { cpu_vcpus, memory_mb, disk_gb: 0, enabled };
}

describe("firstEnabledCombo", () => {
  it("returns the leading combo when the plan enables it", () => {
    // Today's Free plan: the smallest combo (1 vCPU / 1 GB) leads the
    // grid and is the one plan-enabled size, so it's picked directly.
    const caps = makeCaps([
      combo(1, 1024, true),
      combo(1, 2048, false),
      combo(2, 2048, false),
    ]);
    expect(firstEnabledCombo(caps)).toEqual(combo(1, 1024, true));
  });

  it("skips a disabled leading combo and returns the first enabled one", () => {
    // A plan that disables the smallest size: the helper must walk
    // past the disabled leader rather than seed an off-plan default.
    const caps = makeCaps([
      combo(1, 1024, false),
      combo(1, 2048, true),
      combo(2, 2048, false),
    ]);
    const seed = firstEnabledCombo(caps);
    expect(seed?.enabled).not.toBe(false);
    expect([seed?.cpu_vcpus, seed?.memory_mb]).toEqual([1, 2048]);
  });

  it("treats a missing enabled flag as enabled (Team / unrestricted)", () => {
    // When the plan is unrestricted the backend omits the flag, so
    // ``enabled`` is undefined; the first combo (1 vCPU / 1 GB) wins —
    // which is the Team dropdown default.
    const caps = makeCaps([combo(1, 1024), combo(2, 4096)]);
    expect(firstEnabledCombo(caps)).toEqual(combo(1, 1024));
  });

  it("falls back to the first combo when every combo is disabled", () => {
    const caps = makeCaps([combo(1, 1024, false), combo(1, 2048, false)]);
    expect(firstEnabledCombo(caps)).toEqual(combo(1, 1024, false));
  });

  it("returns undefined when the provider advertises no combos", () => {
    expect(firstEnabledCombo(makeCaps([]))).toBeUndefined();
  });
});

describe("isDockerCommand", () => {
  it("matches the bare docker binary, case- and space-insensitively", () => {
    expect(isDockerCommand("docker")).toBe(true);
    expect(isDockerCommand("DOCKER")).toBe(true);
    expect(isDockerCommand("  docker  ")).toBe(true);
  });

  it("matches a full docker command line and a path-prefixed binary", () => {
    expect(isDockerCommand("docker run -i --rm mcp/everything")).toBe(true);
    expect(isDockerCommand("/usr/bin/docker")).toBe(true);
  });

  it("does not match the other runners or empty input", () => {
    expect(isDockerCommand("npx")).toBe(false);
    expect(isDockerCommand("uvx")).toBe(false);
    expect(isDockerCommand("docker-compose")).toBe(false);
    expect(isDockerCommand("")).toBe(false);
    expect(isDockerCommand(null)).toBe(false);
    expect(isDockerCommand(undefined)).toBe(false);
  });
});
