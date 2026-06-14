import { Link } from "react-router";
import { useOrgSlug } from "../hooks/useOrgSlug";

export function RoleBadge({
  role,
  isAdmin,
  orgSlug,
}: {
  role: string;
  isAdmin?: boolean;
  /** Org the role link should target. Defaults to the current org from
   *  the route. Pass it explicitly when a single page lists roles for
   *  several orgs (e.g. the organizations list), so each badge links to
   *  its own org's permissions page rather than the current one. */
  orgSlug?: string;
}) {
  const currentSlug = useOrgSlug();
  const slug = orgSlug ?? currentSlug;
  const label = role === "default" ? "Default" : role;
  return (
    <Link
      to={`/orgs/${slug}/admin/permissions?role=${encodeURIComponent(role)}`}
      className={`inline-block px-1.5 py-0.5 rounded text-xs hover:opacity-80 ${
        isAdmin
          ? "bg-purple-100 text-purple-700"
          : "bg-zinc-100 text-zinc-600"
      }`}
    >
      {label}
    </Link>
  );
}
