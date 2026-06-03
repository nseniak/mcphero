import { useEffect, useState } from "react";
import { Bell, Check, Sparkles, X } from "lucide-react";
import { track } from "../lib/analytics";

export interface UpgradeToTeamDialogProps {
  /** Backend gate identifier (e.g. "max_seats", "max_http_upstreams"). */
  gate: string;
  current: number | null;
  limit: number | null;
  /** Where the dialog was triggered from (for analytics). */
  source: string;
  /** Backend-supplied human-readable headline; falls back to a
   *  gate-aware default when empty. */
  message: string;
  onClose: () => void;
}

const GATE_HEADLINES: Record<string, string> = {
  max_seats: "Your team has hit its teammate limit on Free.",
  max_http_upstreams: "Your team has hit its remote-HTTP-MCP limit on Free.",
  max_stdio_upstreams: "Your team has hit its hosted-stdio-MCP limit on Free.",
  max_custom_roles: "Custom roles aren't available on Free.",
  allow_argument_constraints: "Argument checks aren't available on Free.",
  allowed_sandbox_combos: "Larger sandbox sizes aren't available yet.",
  header_pill: "Ready to roll out across your team?",
  pricing_page_card: "Ready to roll out across your team?",
};

function headline(gate: string, fallback: string): string {
  return GATE_HEADLINES[gate] ?? fallback ?? "Upgrade to Team";
}

export function UpgradeToTeamDialog(props: UpgradeToTeamDialogProps) {
  const { gate, current, limit, source, message, onClose } = props;
  const [notified, setNotified] = useState(false);

  // Mount-time analytics: every time the dialog appears, fire the
  // ``upgrade_to_team_clicked`` event with the gate + source so the
  // upgrade-demand funnel is observable end-to-end.
  useEffect(() => {
    track("upgrade_to_team_clicked", {
      gate,
      source,
      current_value: current,
      limit,
    });
  }, [gate, source, current, limit]);

  function handleNotify(): void {
    track("upgrade_to_team_clicked", {
      gate,
      source,
      intent: "notify_me",
      current_value: current,
      limit,
    });
    setNotified(true);
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/40" onClick={onClose} />
      <div
        role="dialog"
        aria-labelledby="upgrade-to-team-title"
        className="relative bg-white rounded-lg shadow-lg p-6 max-w-md w-full mx-4 space-y-4"
      >
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center justify-center w-8 h-8 rounded-md bg-zinc-900 text-white">
              <Sparkles size={16} />
            </span>
            <h3 id="upgrade-to-team-title" className="text-lg font-semibold text-zinc-900">
              {headline(gate, message)}
            </h3>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-600 transition-colors"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <div className="space-y-3 text-sm text-zinc-600 leading-relaxed">
          <p>
            Team plans aren&apos;t available yet — we&apos;re working on it.
            We&apos;ll let you know as soon as they launch.
          </p>
          {gate === "max_seats" && limit !== null && (
            <p className="text-zinc-500 text-xs">
              You can free up a seat by removing an existing teammate, then
              try again.
            </p>
          )}
        </div>
        {notified ? (
          <div className="flex items-center gap-2 px-3 py-2 bg-green-50 border border-green-200 rounded text-sm text-green-800">
            <Check size={14} className="shrink-0" />
            <span>Thanks! We&apos;ll let you know when Team launches.</span>
          </div>
        ) : null}
        {!notified && (
          <div className="flex justify-end">
            <button
              onClick={handleNotify}
              className="px-4 py-2 bg-zinc-900 text-white text-sm rounded hover:bg-zinc-800 flex items-center gap-1.5"
            >
              <Bell size={14} /> Notify me
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
