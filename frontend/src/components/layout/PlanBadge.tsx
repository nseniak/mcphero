import { useAuth } from "../../hooks/useAuth";
import { track } from "../../lib/analytics";
import { handlePlanLimitError } from "../../lib/planLimits";
import { PlanLimitError } from "../../api/client";
import type { PlanName } from "../../api/types";

interface PlanBadgeProps {
  /** When provided, renders for this plan; otherwise reads the
   *  current session's org plan from ``useAuth()``. */
  plan?: PlanName;
  /** Whether the viewer is admin in the displayed org. When omitted,
   *  falls back to the current session's ``is_admin`` from
   *  ``useAuth()``. The Free pill only shows the upgrade affordance
   *  for admins — non-admins can't act on it, so we render a static
   *  "Free plan" label instead. */
  isAdmin?: boolean;
  /** Analytics source label. Defaults to ``"header_pill"`` to match
   *  the original dashboard-header usage. */
  source?: string;
}

/** "Free plan" / "Team plan" pill rendered in the dashboard header.
 *  On Free + admin, the pill shows "Upgrade" and opens the upgrade
 *  dialog. On Free + non-admin, it's a static "Free plan" label with
 *  no upgrade affordance. On Team it's a static "Team plan".
 *  Pass ``plan`` and ``isAdmin`` to render for an arbitrary org (e.g.
 *  each row of the manage-organizations table). */
export function PlanBadge({
  plan: planProp,
  isAdmin: isAdminProp,
  source = "header_pill",
}: PlanBadgeProps = {}) {
  const { user } = useAuth();
  const plan: PlanName | undefined =
    planProp ?? user?.current_org?.plan;
  const isAdmin: boolean =
    isAdminProp ?? user?.current_org?.is_admin ?? false;
  if (!plan) return null;

  if (plan === "team") {
    return (
      <span
        className="px-2 py-0.5 bg-zinc-900 text-white rounded text-xs font-medium"
        data-testid="plan-badge"
        data-plan="team"
      >
        Team plan
      </span>
    );
  }

  if (!isAdmin) {
    return (
      <span
        className="px-2 py-0.5 bg-zinc-100 text-zinc-600 rounded text-xs font-medium"
        data-testid="plan-badge"
        data-plan="free"
      >
        Free plan
      </span>
    );
  }

  return (
    <button
      type="button"
      data-testid="plan-badge"
      data-plan="free"
      onClick={() => {
        track("plan_badge_clicked", { plan, source });
        // Synthesize a PlanLimitError without a backend round-trip so
        // the badge surface lives in the same upgrade-modal funnel as
        // the post-402 flow.
        handlePlanLimitError(
          new PlanLimitError("Upgrade to Team", source, null, null),
          { source },
        );
      }}
      className="px-2 py-0.5 bg-amber-100 text-amber-900 hover:bg-amber-200 rounded text-xs font-medium"
    >
      Free plan · Upgrade
    </button>
  );
}
