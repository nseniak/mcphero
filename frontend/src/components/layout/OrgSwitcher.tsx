import { Link } from "react-router";
import { useAuth } from "../../hooks/useAuth";
import { useFeatures } from "../../hooks/useFeatures";
import { switchOrg } from "../../api/orgs";
import { track } from "../../lib/analytics";

/**
 * Org switcher dropdown shown in the dashboard header.
 *
 * In cloud mode, always visible — shows current org, lets users
 * switch between orgs, and links to the org management page.
 * Hidden in standalone mode.
 */
export function OrgSwitcher() {
  const { user } = useAuth();
  const { mode } = useFeatures();

  if (mode === "standalone" || !user) return null;
  if (!user.current_org) return null;

  async function handleChange(value: string) {
    const fromSlug = user!.current_org!.slug;
    if (value === fromSlug) return;
    try {
      await switchOrg(value);
      track("org_switched", { from_org_slug: fromSlug, to_org_slug: value });
      // Hand off to DefaultRedirect so the landing page is picked by
      // the user's role in the *target* org (admin → /admin/upstream,
      // non-admin → /my-tools or /connect). Hard-coding /admin/upstream
      // here 403s when the user isn't admin in the target org.
      window.location.href = "/app";
    } catch {
      // Swallow — the user will see the old org and can retry
    }
  }

  return (
    <div className="flex items-center gap-2">
      {user.orgs.length > 1 ? (
        <select
          value={user.current_org.slug}
          onChange={(e) => handleChange(e.target.value)}
          className="text-sm border border-zinc-200 rounded-md px-2 py-1 bg-white text-zinc-700 focus:outline-none focus:ring-2 focus:ring-zinc-900"
        >
          {user.orgs.map((org) => (
            <option key={org.slug} value={org.slug}>
              {org.display_name}
            </option>
          ))}
        </select>
      ) : (
        <span className="text-sm text-zinc-500 font-medium">
          {user.current_org.display_name}
        </span>
      )}
      <Link
        to="/orgs/manage"
        className="text-xs text-zinc-400 hover:text-zinc-600 hover:underline"
      >
        Manage organizations
      </Link>
    </div>
  );
}
