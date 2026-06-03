import { useUpstreamActions } from "../hooks/useUpstreamActions";
import { useTranslation, type TranslationKey } from "../i18n/index";
import {
  Loader2,
  Unplug,
  Square,
  KeyRound,
  Play,
  PlugZap,
} from "lucide-react";
import { ActionButton } from "./ui/action-button";

interface UpstreamActionButtonsProps {
  id: string;
  /** Org-level Ready — admin authenticated for OAuth modes, shared
   *  session live for service_account. */
  ready: boolean;
  transport: string;
  authMode: string;
  /** True iff a fire-and-forget admin reconnect is in flight on the
   *  backend (``UpstreamSummary.starting`` / ``UpstreamDetail.starting``).
   *  Renders a disabled "Starting…" button regardless of which admin
   *  triggered it, reconstructing the spinner from server truth on
   *  tab switch / reload. Survives the local ``busyAction`` clearing
   *  (the HTTP response returns in ms; the real connect can take 1–60s
   *  for a sandbox cold pull). */
  starting?: boolean;

  reload: () => void;
  /** Forwarded to ``useUpstreamActions``. Fires synchronously on
   *  Start / Connect click so the parent can clear stale UI
   *  (disconnect_reason banner, Server logs scrollback) ahead of
   *  the API roundtrip. */
  onOptimisticReset?: () => void;
}

function stopLabel(transport: string): { label: TranslationKey; busy: TranslationKey } {
  if (transport === "stdio") return { label: "common.stop", busy: "common.stopping" };
  return { label: "common.disconnect", busy: "common.disconnecting" };
}

function startLabel(transport: string): { label: TranslationKey; busy: TranslationKey; icon: typeof Play } {
  if (transport === "stdio") return { label: "common.start", busy: "common.starting", icon: Play };
  return { label: "common.connect", busy: "common.connecting", icon: PlugZap };
}

export function UpstreamActionButtons({
  id,
  ready,
  transport,
  authMode,
  starting,
  reload,
  onOptimisticReset,
}: UpstreamActionButtonsProps) {
  const { t } = useTranslation();
  const { busyAction, handleConnect, handleDisconnect, handleReconnect } =
    useUpstreamActions({ id, reload, onOptimisticReset });

  const isOAuth = authMode === "admin_oauth" || authMode === "per_user_oauth";
  // Server-driven "Starting…": after the fire-and-forget reconnect
  // returns, ``busyAction`` clears within MIN_DELAY but the underlying
  // sandbox is still cold-pulling. ``UpstreamSummary.starting`` (server
  // truth) keeps the disabled pill visible until the bg connect task
  // completes — so a second admin watching the same upstream sees the
  // same pill, and a tab refresh mid-cold-pull reconstructs it without
  // another click.
  const serverStarting = !ready && !!starting;
  const isBusy = busyAction !== null || serverStarting;

  // Not Ready: OAuth modes ⇒ Authenticate; service_account ⇒
  // Connect/Start (no OAuth flow needed, just open the session).
  if (!ready) {
    if (isOAuth) {
      return (
        <ActionButton variant="success" onClick={handleConnect} disabled={isBusy}>
          {busyAction === "connect" ? <Loader2 size={12} className="animate-spin" /> : <KeyRound size={12} />}
          {busyAction === "connect" ? t("common.connecting") : t("common.authenticate")}
        </ActionButton>
      );
    }
    const start = startLabel(transport);
    const StartIcon = start.icon;
    const showStarting = busyAction === "reconnect" || serverStarting;
    return (
      <ActionButton onClick={handleReconnect} disabled={isBusy}>
        {showStarting ? <Loader2 size={12} className="animate-spin" /> : <StartIcon size={12} />}
        {showStarting ? t(start.busy) : t(start.label)}
      </ActionButton>
    );
  }

  // Ready: always Disconnect, regardless of who owns the slot.
  // For OAuth modes the admin-tab disconnect endpoint clears the
  // slot owner's row (whoever they are) — so a second admin who
  // wants to claim the slot just clicks Disconnect, then the
  // Authenticate button that appears next. The slot owner's
  // identity is surfaced inside the Status pill ("Connected by
  // <email>") rather than a special take-over button here.
  const stop = stopLabel(transport);
  return (
    <ActionButton variant="warning" onClick={handleDisconnect} disabled={isBusy}>
      {busyAction === "disconnect" ? <Loader2 size={12} className="animate-spin" /> : transport === "stdio" ? <Square size={12} /> : <Unplug size={12} />}
      {busyAction === "disconnect" ? t(stop.busy) : t(stop.label)}
    </ActionButton>
  );
}
