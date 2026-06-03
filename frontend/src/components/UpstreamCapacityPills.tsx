import type { PlanName } from "../api/types";
import { PLAN_UPSTREAM_LIMITS } from "../lib/planLimits";

interface UpstreamCapacityPillsProps {
  plan: PlanName;
  httpCount: number;
  stdioCount: number;
  /** "compact" hides the per-pill label letters (used in narrow
   *  surfaces like the sidebar nav row). Default "default" matches
   *  the manage-organizations table. */
  density?: "default" | "compact";
}

interface PillProps {
  count: number;
  cap: number | null;
  label: string;
  density: "default" | "compact";
}

function Pill({ count, cap, label, density }: PillProps) {
  const atCap = cap !== null && count >= cap;
  const overCap = cap !== null && count > cap;
  // Capacity-meter colour: zinc when below, amber at-cap, red over-cap
  // (over-cap can happen after a plan flip back to Free with leftover
  // data; the badge is the cue the org has to delete or upgrade).
  const palette = overCap
    ? "bg-red-100 text-red-800"
    : atCap
    ? "bg-amber-100 text-amber-800"
    : count === 0
    ? "bg-zinc-100 text-zinc-400"
    : "bg-blue-50 text-blue-700";
  const denominator = cap === null ? "" : `/${cap}`;
  const py = density === "compact" ? "py-0" : "py-0.5";
  return (
    <span
      className={
        `inline-flex items-center gap-1 px-1.5 ${py} ` +
        `rounded text-[10px] font-medium tabular-nums ${palette}`
      }
    >
      <span>
        {count}
        {denominator}
      </span>
      <span>{label}</span>
    </span>
  );
}

/** Two-pill capacity meter for an org's upstream usage.
 *
 *  On Free: ``2/5 http`` ``0/1 stdio`` (denominator is the plan cap).
 *  On Team: ``2 http`` ``0 stdio`` (no fraction — unlimited).
 *
 *  Used in:
 *  - the dashboard sidebar's "Upstream MCPs" row (compact density)
 *  - the manage-organizations table (default density). */
export function UpstreamCapacityPills({
  plan,
  httpCount,
  stdioCount,
  density = "default",
}: UpstreamCapacityPillsProps) {
  const limits = PLAN_UPSTREAM_LIMITS[plan];
  return (
    <span className="inline-flex items-center gap-1">
      <Pill
        count={httpCount}
        cap={limits.http}
        label="http"
        density={density}
      />
      <Pill
        count={stdioCount}
        cap={limits.stdio}
        label="stdio"
        density={density}
      />
    </span>
  );
}
