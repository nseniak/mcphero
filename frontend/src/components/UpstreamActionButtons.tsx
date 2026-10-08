import { useUpstreamActions } from "../hooks/useUpstreamActions";
import { useTranslation, type TranslationKey } from "../i18n/index";
import {
  Loader2,
  LogOut,
  Unplug,
  Square,
  KeyRound,
  Play,
  PlugZap,
} from "lucide-react";
import { ConfirmDialog, useConfirm } from "./ConfirmDialog";
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
  /** An admin's Stop holds (``UpstreamSummary.stopped``). */
  stopped?: boolean;
  /** Admin whose saved sign-in serves an OAuth MCP, or, while stopped,
   *  the one Start reuses (``UpstreamSummary.slot_owner``). */
  slotOwner?: string | null;

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
  stopped,
  slotOwner,
  reload,
  onOptimisticReset,
}: UpstreamActionButtonsProps) {
  const { t } = useTranslation();
  const {
    busyAction,
    handleConnect,
    handleDisconnect,
    handleReconnect,
    handleSignOut,
  } = useUpstreamActions({ id, reload, onOptimisticReset });
  const { confirm, dialogProps } = useConfirm();

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

  const stop = stopLabel(transport);
  const stopButton = (
    <ActionButton variant="warning" onClick={handleDisconnect} disabled={isBusy}>
      {busyAction === "disconnect" ? <Loader2 size={12} className="animate-spin" /> : transport === "stdio" ? <Square size={12} /> : <Unplug size={12} />}
      {busyAction === "disconnect" ? t(stop.busy) : t(stop.label)}
    </ActionButton>
  );

  // Remove sign-in deletes the admin sign-in that serves the MCP,
  // whoever holds it, so another admin can sign in with Authenticate
  // (the take-over: Disconnect keeps every sign-in). Not "Sign out":
  // the page header already has that, for leaving MCP Hero. The dialog
  // names the admin, and the request carries that name, so a sign-in
  // that changed meanwhile is never the one removed.
  const onRemoveSignIn = async () => {
    if (!slotOwner) return;
    const ok = await confirm({
      title: t("upstreams.removeSignIn"),
      message: t("upstreams.confirmSignOutOwner", { email: slotOwner }),
      confirmLabel: t("upstreams.removeSignIn"),
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    const error = await handleSignOut(slotOwner);
    if (error) {
      await confirm({
        title: t("upstreams.removeSignIn"),
        message: error,
        confirmLabel: t("common.close"),
        cancelLabel: "",
      });
    }
  };
  const removeSignInButton = isOAuth && slotOwner ? (
    <ActionButton onClick={onRemoveSignIn} disabled={isBusy}>
      {busyAction === "signout" ? <Loader2 size={12} className="animate-spin" /> : <LogOut size={12} />}
      {busyAction === "signout" ? t("upstreams.removingSignIn") : t("upstreams.removeSignIn")}
    </ActionButton>
  ) : null;

  return (
    <>
      {renderMain()}
      {removeSignInButton}
      <ConfirmDialog {...dialogProps} />
    </>
  );

  function renderMain() {
    // Ready: always Disconnect, regardless of who owns the slot. It
    // closes every live session and keeps every saved sign-in, so the
    // Connect that appears next brings the MCP back with no sign-in.
    // The slot owner's identity is surfaced inside the Status pill
    // ("Ready, by <email>").
    if (ready) return stopButton;
    if (isOAuth) {
      // Stopped with the admin sign-in kept: Start reuses it.
      if (stopped && slotOwner) {
        return (
          <ActionButton variant="success" onClick={handleConnect} disabled={isBusy}>
            {busyAction === "connect" ? <Loader2 size={12} className="animate-spin" /> : <PlugZap size={12} />}
            {busyAction === "connect" ? t("common.connecting") : t("common.connect")}
          </ActionButton>
        );
      }
      const authenticate = (
        <ActionButton variant="success" onClick={handleConnect} disabled={isBusy}>
          {busyAction === "connect" ? <Loader2 size={12} className="animate-spin" /> : <KeyRound size={12} />}
          {busyAction === "connect" ? t("common.connecting") : t("common.authenticate")}
        </ActionButton>
      );
      // Running without an admin sign-in (members' own sign-ins may
      // still serve calls): it can still be stopped.
      if (!stopped) {
        return (
          <>
            {authenticate}
            {stopButton}
          </>
        );
      }
      return authenticate;
    }
    // service_account ⇒ Connect/Start (no OAuth flow needed, just open
    // the session).
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
}
