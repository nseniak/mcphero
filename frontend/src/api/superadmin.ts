/** Superadmin dashboard endpoints (cross-org browse + soft actions).
 *
 * All routes under ``/api/superadmin/*`` are gated by membership in
 * ``MCPOLIS_SUPERADMIN_EMAILS``. Non-superadmins get 403; the routing
 * layer ``SuperadminGuard`` redirects away non-superadmins before any
 * request fires, so a 403 here means state has drifted.
 */
import { apiFetch } from "./client";
import type {
  SuperadminAuditAggregatesResponse,
  SuperadminAuditSearchResponse,
  SuperadminAuthHealthResponse,
  SuperadminOrgListResponse,
  SuperadminSubscriptionUpdateResponse,
  SuperadminOverviewResponse,
  SuperadminReauthResponse,
  SuperadminSessionsRevokedResponse,
  SuperadminSystemResponse,
  SuperadminUpstreamListResponse,
  SuperadminUserDetailResponse,
  SuperadminUserListResponse,
} from "./types";

export interface SuperadminAuditFilters {
  org_id?: string;
  user_id?: string;
  upstream_id?: string;
  tool?: string;
  action?: string;
}

export function fetchSuperadminOverview(): Promise<SuperadminOverviewResponse> {
  return apiFetch<SuperadminOverviewResponse>("/api/superadmin/overview");
}

export function fetchSuperadminOrgs(): Promise<SuperadminOrgListResponse> {
  return apiFetch<SuperadminOrgListResponse>("/api/superadmin/orgs");
}

export function updateSuperadminOrgSubscription(
  orgId: string,
  plan: "free" | "team",
): Promise<SuperadminSubscriptionUpdateResponse> {
  return apiFetch<SuperadminSubscriptionUpdateResponse>(
    `/api/superadmin/orgs/${encodeURIComponent(orgId)}/subscription`,
    { method: "PATCH", body: JSON.stringify({ plan }) },
  );
}

export function fetchSuperadminUsers(): Promise<SuperadminUserListResponse> {
  return apiFetch<SuperadminUserListResponse>("/api/superadmin/users");
}

export function fetchSuperadminUser(
  email: string,
): Promise<SuperadminUserDetailResponse> {
  return apiFetch<SuperadminUserDetailResponse>(
    `/api/superadmin/users/${encodeURIComponent(email)}`,
  );
}

export function fetchSuperadminUpstreams(): Promise<SuperadminUpstreamListResponse> {
  return apiFetch<SuperadminUpstreamListResponse>(
    "/api/superadmin/upstreams",
  );
}

function buildAuditQuery(
  filters: SuperadminAuditFilters,
  extra: Record<string, string | number> = {},
): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v) params.set(k, v);
  }
  for (const [k, v] of Object.entries(extra)) {
    params.set(k, String(v));
  }
  const q = params.toString();
  return q ? `?${q}` : "";
}

export function fetchSuperadminAudit(
  filters: SuperadminAuditFilters = {},
  limit: number = 100,
  offset: number = 0,
): Promise<SuperadminAuditSearchResponse> {
  const q = buildAuditQuery(filters, { limit, offset });
  return apiFetch<SuperadminAuditSearchResponse>(
    `/api/superadmin/audit${q}`,
  );
}

export function fetchSuperadminAuditAggregates(
  filters: SuperadminAuditFilters = {},
  sampleSize: number = 2000,
): Promise<SuperadminAuditAggregatesResponse> {
  const q = buildAuditQuery(filters, { sample_size: sampleSize });
  return apiFetch<SuperadminAuditAggregatesResponse>(
    `/api/superadmin/audit/aggregates${q}`,
  );
}

export function fetchSuperadminAuthHealth(): Promise<SuperadminAuthHealthResponse> {
  return apiFetch<SuperadminAuthHealthResponse>(
    "/api/superadmin/auth-health",
  );
}

export function fetchSuperadminSystem(): Promise<SuperadminSystemResponse> {
  return apiFetch<SuperadminSystemResponse>("/api/superadmin/system");
}

export function revokeSuperadminUserSessions(
  email: string,
): Promise<SuperadminSessionsRevokedResponse> {
  return apiFetch<SuperadminSessionsRevokedResponse>(
    `/api/superadmin/users/${encodeURIComponent(email)}/sessions/revoke`,
    { method: "POST" },
  );
}

export function triggerSuperadminReauth(
  email: string,
  orgId: string,
  upstreamId: string,
): Promise<SuperadminReauthResponse> {
  const path = `/api/superadmin/users/${encodeURIComponent(email)}/connections/${encodeURIComponent(orgId)}/${encodeURIComponent(upstreamId)}/reauth`;
  return apiFetch<SuperadminReauthResponse>(path, { method: "POST" });
}
