import { useTranslation } from "../i18n/index";

import { AUTH_DISCONNECT_REASONS } from "../api/types";

interface StatusBadgeProps {
  connected: boolean;
  disconnectReason?: string | null;
  label?: string;
  /** True iff a fire-and-forget admin reconnect is in flight on the
   *  backend (``UpstreamSummary.starting`` / ``UpstreamDetail.starting``).
   *  When set and ``connected`` is still false, the pill renders a
   *  pulsing "Starting…" instead of "Disconnected" so the user sees
   *  that something IS happening during the (1–60s) sandbox cold
   *  pull. Server truth — survives tab switches and reloads, and a
   *  second admin watching the same upstream sees the same pill. */
  starting?: boolean;
}

export function StatusBadge({
  connected,
  disconnectReason,
  label,
  starting,
}: StatusBadgeProps) {
  const { t } = useTranslation();

  // Surface the fire-and-forget reconnect even before ``connected``
  // flips. Same color family as "disconnected" (zinc) but with a
  // pulsing dot to signal that work is actively happening — mirrors
  // the OAuth pattern where "Authenticate" → "Connecting…" → Ready.
  const isStarting = !connected && !!starting;

  let text: string;
  if (label) {
    text = label;
  } else if (connected) {
    text = t("status.connected");
  } else if (isStarting) {
    text = t("status.starting");
  } else if (disconnectReason && !AUTH_DISCONNECT_REASONS.has(disconnectReason)) {
    text = t("status.couldntConnect");
  } else {
    text = t("status.disconnected");
  }

  const hasError =
    !connected && !!disconnectReason && !AUTH_DISCONNECT_REASONS.has(disconnectReason);

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium max-w-full align-middle ${
        connected
          ? "bg-green-100 text-green-700"
          : hasError
            ? "bg-red-100 text-red-700"
            : isStarting
              ? "bg-amber-100 text-amber-700"
              : "bg-zinc-100 text-zinc-500"
      }`}
      title={text}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${
          connected
            ? "bg-green-500"
            : hasError
              ? "bg-red-500"
              : isStarting
                ? "bg-amber-500 animate-pulse"
                : "bg-zinc-400"
        }`}
      />
      <span className="truncate min-w-0">{text}</span>
    </span>
  );
}
