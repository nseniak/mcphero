/**
 * Per-MCP Sandbox files management API.
 *
 * Files are uploaded per-upstream and written into the sandbox at
 * the configured ``target_path`` before the MCP process starts.
 * File names do NOT participate in ``${...}`` substitution — they
 * live in their own namespace. ``target_path`` itself accepts the
 * same ``${...}`` references env-var values accept (system + user
 * Variables); operators wire a file to an env var by typing the
 * same ``${HOME}/...`` literal on both sides.
 */
import { apiFetch } from "./client";
import type { SandboxFileSummary, SystemVariable } from "./types";

export function listSandboxFiles(upstreamId: string): Promise<SandboxFileSummary[]> {
  return apiFetch<SandboxFileSummary[]>(
    `/api/admin/upstreams/${encodeURIComponent(upstreamId)}/sandbox-files`,
  );
}

export function setSandboxFile(
  upstreamId: string,
  name: string,
  contents: string,
  targetPath: string,
  displayName?: string,
): Promise<SandboxFileSummary> {
  const body: Record<string, string> = {
    contents,
    target_path: targetPath,
  };
  if (displayName !== undefined && displayName.trim() !== "") {
    body.display_name = displayName;
  }
  return apiFetch<SandboxFileSummary>(
    `/api/admin/upstreams/${encodeURIComponent(upstreamId)}/sandbox-files/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
}

export function deleteSandboxFile(
  upstreamId: string,
  name: string,
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(
    `/api/admin/upstreams/${encodeURIComponent(upstreamId)}/sandbox-files/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

/** Read-only system Variables (e.g. ``${HOME}``) for the given
 *  transport. Stdio returns the sandbox-injected set; HTTP returns
 *  ``[]`` (no sandbox filesystem). Keyed on transport rather than
 *  upstream id so the create wizard can call it before an upstream
 *  exists. */
export function listSystemVariables(
  transport: "stdio" | "streamable_http",
): Promise<SystemVariable[]> {
  return apiFetch<SystemVariable[]>(
    `/api/admin/system-variables?transport=${encodeURIComponent(transport)}`,
  );
}
