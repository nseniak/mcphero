/**
 * Per-MCP environment-variable management API.
 *
 * The bucket holds two flavours:
 * - ``is_secret=true`` (the default): plaintext is write-only —
 *   accepted on PUT but never returned. Listing carries ``last_four``
 *   for display only.
 * - ``is_secret=false``: value is returned in clear by GET / list so
 *   the UI can render it verbatim.
 *
 * The toggle is a **create-time** decision: the backend repository
 * preserves the existing record's ``is_secret`` on replace, regardless
 * of the body. To flip a value's secrecy after the fact, delete +
 * re-create.
 */
import { apiFetch } from "./client";
import type { TemplateVarSummary } from "./types";

export function listTemplateVars(upstreamId: string): Promise<TemplateVarSummary[]> {
  return apiFetch<TemplateVarSummary[]>(
    `/api/admin/upstreams/${encodeURIComponent(upstreamId)}/template-vars`,
  );
}

export function setTemplateVar(
  upstreamId: string,
  name: string,
  value: string,
  isSecret: boolean,
): Promise<TemplateVarSummary> {
  return apiFetch<TemplateVarSummary>(
    `/api/admin/upstreams/${encodeURIComponent(upstreamId)}/template-vars/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      body: JSON.stringify({ value, is_secret: isSecret }),
    },
  );
}

export function deleteTemplateVar(
  upstreamId: string,
  name: string,
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(
    `/api/admin/upstreams/${encodeURIComponent(upstreamId)}/template-vars/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}
