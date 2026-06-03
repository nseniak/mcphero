import { useParams } from "react-router";
import { useAuth } from "./useAuth";

/**
 * Returns the current org slug for URL building.
 *
 * Reads from the URL `:slug` param (preferred), falls back to
 * `user.current_org.slug`, then to `"default"` (standalone).
 */
export function useOrgSlug(): string {
  const { slug } = useParams<{ slug: string }>();
  const { user } = useAuth();
  return slug ?? user?.current_org?.slug ?? "default";
}

/**
 * Build an org-prefixed path under `/orgs/{slug}`.
 *
 * `useOrgPath("/admin/upstream")` → `"/orgs/acme-corp/admin/upstream"`
 */
export function useOrgPath(path: string): string {
  const slug = useOrgSlug();
  return `/orgs/${slug}${path}`;
}
