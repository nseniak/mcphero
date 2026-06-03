/**
 * Detail-page banner that surfaces when an upstream's running session
 * has drifted from the saved config. Replaces the legacy
 * post-save ``RestartRequiredDialog`` modal.
 *
 * Visibility rules (decided by the parent — this component is dumb):
 *   - ``ready === true`` (a running session exists)
 *   - ``is_dirty === true`` from the upstream-detail response
 *   - the user hasn't dismissed *this specific config_hash* yet
 *
 * Dismissal is per-MCP and ephemeral: keyed by ``upstreamId`` AND
 * ``configHash`` in **sessionStorage** (not localStorage), so a tab
 * close re-arms it. Crucially the key includes the hash, so a fresh
 * save (which produces a new hash) auto-invalidates the dismissal —
 * the user sees the banner again without having to clear anything.
 */
import type { JSX } from "react";
import { useEffect, useState } from "react";
import { Info, X } from "lucide-react";

const KEY_PREFIX = "mcpolis:dirty-dismissed:";

function dismissalKey(upstreamId: string, configHash: string): string {
  return `${KEY_PREFIX}${upstreamId}:${configHash}`;
}

function readDismissed(upstreamId: string, configHash: string): boolean {
  try {
    return sessionStorage.getItem(dismissalKey(upstreamId, configHash)) === "true";
  } catch {
    return false;
  }
}

function writeDismissed(upstreamId: string, configHash: string): void {
  try {
    sessionStorage.setItem(dismissalKey(upstreamId, configHash), "true");
  } catch {
    // SessionStorage might be disabled (e.g. private browsing in some
    // browsers). Failing closed is fine — the banner just keeps showing.
  }
}

interface Props {
  /** Hidden when ``false`` regardless of dirty state — the banner is
   *  meaningless without a running session to drift from. */
  visible: boolean;
  upstreamId: string;
  /** SHA-256 hex of the current saved config, from the detail
   *  response. Drives the per-hash sessionStorage dismissal key. */
  configHash: string;
  /** Drives the verb pair in the banner copy: stdio MCPs run as a
   *  subprocess (Stop / Start), HTTP MCPs are session-only
   *  (Disconnect / Reconnect). The wrong verb confuses operators
   *  who scan the page for the matching action button. */
  transport: string;
}

export function DirtyConfigBanner({
  visible,
  upstreamId,
  configHash,
  transport,
}: Props): JSX.Element | null {
  // Re-check the dismissal flag whenever the hash changes — a save
  // mid-session produces a new hash, so the dismissal flag for the
  // previous hash no longer applies and the banner reappears.
  const [dismissed, setDismissed] = useState(() =>
    readDismissed(upstreamId, configHash),
  );
  useEffect(() => {
    setDismissed(readDismissed(upstreamId, configHash));
  }, [upstreamId, configHash]);

  if (!visible || dismissed) return null;

  const handleDismiss = (): void => {
    writeDismissed(upstreamId, configHash);
    setDismissed(true);
  };

  const verbs =
    transport === "stdio" ? "stop and start" : "disconnect and reconnect";

  return (
    <div
      role="status"
      className="rounded border border-amber-300 bg-amber-50 p-3 mb-4 flex items-start justify-between gap-3"
    >
      <div className="flex items-start gap-2 min-w-0">
        <Info size={16} className="text-amber-600 shrink-0 mt-0.5" />
        <p className="text-sm text-amber-900">
          Configuration has been modified. Changes won't take effect
          until you {verbs} this MCP.
        </p>
      </div>
      <button
        type="button"
        onClick={handleDismiss}
        aria-label="Dismiss"
        className="p-1 text-amber-700 hover:text-amber-900 hover:bg-amber-100 rounded shrink-0"
        title="Dismiss"
      >
        <X size={14} />
      </button>
    </div>
  );
}
