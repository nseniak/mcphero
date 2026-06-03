/** Sandbox-runner admin endpoints.
 *
 * The per-upstream resource update (``PUT /upstreams/{id}/sandbox/resources``)
 * was folded into the unified ``updateUpstream`` body
 * (``sandbox_resources`` field) so the SETTINGS Save button commits
 * display-name + auth + JSON + env vars + resources atomically. */
import { apiFetch } from "./client";
import type { SandboxCapabilitiesResponse } from "./types";

/** Active provider's CPU/RAM/disk grid. The admin UI's per-MCP
 *  resource picker fetches this once per page-load and renders
 *  dropdowns constrained to ``allowed_cpu_vcpus`` ×
 *  ``allowed_memory_mb`` (× ``allowed_disk_gb`` if non-empty). */
export function fetchSandboxCapabilities(): Promise<SandboxCapabilitiesResponse> {
  return apiFetch<SandboxCapabilitiesResponse>(
    "/api/admin/sandbox/capabilities",
  );
}
