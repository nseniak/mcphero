/**
 * Frontend wiring for plan-limit (HTTP 402) responses.
 *
 * The backend's FastAPI exception handler returns a structured JSON
 * envelope on 402 (see ``PlanLimitExceeded`` in the backend); the
 * shared ``apiFetch`` resolves those into a typed ``PlanLimitError``.
 * Mutation handlers throughout the app catch that subclass with
 * ``instanceof`` and call :func:`handlePlanLimitError` — the single
 * place that opens the ``UpgradeToTeamDialog``.
 *
 * The dialog itself is mounted at the app shell. It registers a
 * setter via ``setUpgradeDialogOpener`` on mount; the helper writes
 * to that setter so mutation handlers don't need a ref to the
 * top-level state.
 */
import { PlanLimitError } from "../api/client";
import type { PlanName } from "../api/types";
import { track } from "./analytics";

/** Per-plan upstream caps mirrored from the backend's plan_policy.py.
 *  ``null`` means no cap (Team). Source-of-truth lives backend-side
 *  in ``mcpolis.domain.services.plan_policy``; if the plan widens
 *  there, mirror it here too. */
export const PLAN_UPSTREAM_LIMITS: Record<
  PlanName,
  { http: number | null; stdio: number | null }
> = {
  free: { http: 5, stdio: 1 },
  team: { http: null, stdio: null },
};

export interface PlanLimitDialogState {
  gate: string;
  current: number | null;
  limit: number | null;
  source: string;
  message: string;
}

type Opener = (state: PlanLimitDialogState) => void;

let _opener: Opener | null = null;

/** Wire the app-shell-mounted dialog into the helper. Call once on
 *  mount; pass ``null`` on unmount to detach. */
export function setUpgradeDialogOpener(opener: Opener | null): void {
  _opener = opener;
}

/** Open the upgrade dialog for a known PlanLimitError instance.
 *  Fires the ``upgrade_to_team_clicked`` event with the gate,
 *  source, and current/limit values so we can see which surfaces
 *  drive the most upgrade demand. */
export function handlePlanLimitError(
  err: PlanLimitError,
  opts: { source: string },
): void {
  track("upgrade_to_team_clicked", {
    gate: err.gate,
    source: opts.source,
    current_value: err.current,
    limit: err.limit,
  });
  if (_opener) {
    _opener({
      gate: err.gate,
      current: err.current,
      limit: err.limit,
      source: opts.source,
      message: err.message,
    });
  }
}

/** Convenience helper: if ``err`` is a PlanLimitError, route to the
 *  upgrade modal and return ``true``; otherwise return ``false`` and
 *  let the caller fall through to its default error path. */
export function maybeHandlePlanLimit(
  err: unknown,
  opts: { source: string },
): err is PlanLimitError {
  if (err instanceof PlanLimitError) {
    handlePlanLimitError(err, opts);
    return true;
  }
  return false;
}
