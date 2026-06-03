import { apiFetch } from "./client";
import type { OrgListEntry } from "./types";

export interface ListOrgsResponse {
  orgs: OrgListEntry[];
  current_slug: string | null;
}

export function fetchOrgs(): Promise<ListOrgsResponse> {
  return apiFetch<ListOrgsResponse>("/api/orgs");
}

export type OrgCreatedVia = "signup" | "manage_page";

export function createOrg(
  slug: string,
  displayName: string,
  createdVia: OrgCreatedVia = "manage_page",
): Promise<OrgListEntry> {
  return apiFetch<OrgListEntry>("/api/orgs", {
    method: "POST",
    body: JSON.stringify({
      slug,
      display_name: displayName,
      created_via: createdVia,
    }),
    headers: { "Content-Type": "application/json" },
  });
}

export function switchOrg(slug: string): Promise<void> {
  return apiFetch<void>(`/api/orgs/${encodeURIComponent(slug)}/switch`, {
    method: "POST",
  });
}

export interface OrgInfo {
  slug: string;
  display_name: string;
  member_count: number;
}

export function fetchOrgInfo(slug: string): Promise<OrgInfo> {
  return apiFetch<OrgInfo>(`/api/orgs/${encodeURIComponent(slug)}/info`);
}

export interface PublicOrgInfo {
  exists: boolean;
  display_name: string | null;
}

// Unauthenticated lookup used by the invite-link landing (JoinPage).
// Returns ``{exists: false}`` for unknown slugs instead of throwing —
// the caller distinguishes "valid invite link" from "broken link".
export function fetchPublicOrgInfo(slug: string): Promise<PublicOrgInfo> {
  return apiFetch<PublicOrgInfo>(
    `/api/orgs/${encodeURIComponent(slug)}/public`,
  );
}

export interface SlugCheck {
  available: boolean;
  reason: string | null;
}

export function checkSlug(slug: string): Promise<SlugCheck> {
  return apiFetch<SlugCheck>(`/api/orgs/check-slug/${encodeURIComponent(slug)}`);
}

export function deleteOrg(slug: string): Promise<void> {
  return apiFetch<void>(`/api/orgs/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
}
