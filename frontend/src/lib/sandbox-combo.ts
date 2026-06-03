import type {
  SandboxCapabilitiesResponse,
  SandboxResourceCombo,
} from "../api/types";

/** Whether a stdio command resolves to the docker runner.
 *
 * Mirrors the backend's ``language_for_command`` (template_grid.py): the
 * bare ``docker`` binary maps to the docker (dind) template. Tolerant of
 * a path prefix (``/usr/bin/docker``) and of an argv tail pasted into the
 * same string (``docker run …``) so it matches whether the command field
 * holds just ``docker`` or a whole command line.
 *
 * Used only to surface the below-floor sizing warning in the resource
 * picker — the backend publishes all 8 docker sizes, so this is advisory,
 * never blocking. */
export function isDockerCommand(command: string | null | undefined): boolean {
  if (!command) return false;
  const head = command.trim().toLowerCase().split(/\s+/)[0] ?? "";
  const bin = head.split("/").pop() ?? head;
  return bin === "docker";
}

/** The combo the add-upstream wizard should pre-select.
 *
 * Picks the first PLAN-ENABLED triple from ``allowed_combinations``.
 * The per-axis ``allowed_cpu_vcpus`` / ``allowed_memory_mb`` lists are
 * NOT plan-filtered, and the two dropdowns they used to feed let the
 * user assemble any (cpu, ram) pair — including off-plan ones the
 * backend gate then rejects on submit with a 402, blocking the add
 * flow before the user touches anything. ``allowed_combinations``
 * carries the ``enabled`` flag, so the first enabled triple is always
 * a combo the active plan accepts.
 *
 * Falls back to the first combo when none are enabled (defensive — the
 * picker still renders that option disabled). Returns ``undefined``
 * only when the provider advertises no combos at all. */
export function firstEnabledCombo(
  caps: SandboxCapabilitiesResponse,
): SandboxResourceCombo | undefined {
  return (
    caps.allowed_combinations.find((c) => c.enabled !== false) ??
    caps.allowed_combinations[0]
  );
}
