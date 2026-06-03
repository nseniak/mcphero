import { useState } from "react";
import {
  connectUpstream,
  disconnectUpstream,
  reconnectUpstream,
} from "../api/admin";
import { useOAuthPopup } from "./useOAuthPopup";

export type BusyAction = "connect" | "disconnect" | "reconnect" | null;

const MIN_DELAY = 1000;

interface UseUpstreamActionsOptions {
  id: string;
  reload: () => void;
  /**
   * Synchronous, optimistic UI hook fired the instant the user
   * clicks Start (Reconnect). The parent should clear any
   * previous-attempt error banner + reset the LogViewer scrollback
   * here so the operator gets immediate visual feedback that the
   * old state is gone — without waiting for the ``MIN_DELAY=1000ms``
   * busy window + the server reconnect roundtrip. Without this,
   * a fast-failing command (e.g. python ``SyntaxError`` at parse
   * time, ~500ms) finishes before the post-MIN_DELAY ``reload()``
   * fires, so the cleared state is never observable in React state
   * and the user perceives "nothing happened on click."
   */
  onOptimisticReset?: () => void;
  onError?: (error: string) => void;
}

export function useUpstreamActions({
  id,
  reload,
  onOptimisticReset,
  onError,
}: UseUpstreamActionsOptions) {
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [connectError, setConnectError] = useState<string | null>(null);

  const { startOAuthFlow } = useOAuthPopup({
    onSuccess: () => {
      setBusyAction(null);
      reload();
    },
    onError: (error) => {
      setConnectError(error);
      onError?.(error);
      setBusyAction(null);
      reload();
    },
  });

  const handleConnect = async () => {
    setBusyAction("connect");
    setConnectError(null);
    onOptimisticReset?.();
    const minDelay = new Promise((r) => setTimeout(r, MIN_DELAY));
    try {
      const [result] = await Promise.all([connectUpstream(id), minDelay]);
      if (!startOAuthFlow(id, result, () => connectUpstream(id))) return;
      if (result.connected) {
        reload();
      } else if (result.error) {
        setConnectError(result.error);
        reload();
      } else {
        reload();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Connection failed";
      setConnectError(msg);
      reload();
    }
    setBusyAction(null);
  };

  const handleDisconnect = async () => {
    setBusyAction("disconnect");
    const minDelay = new Promise((r) => setTimeout(r, MIN_DELAY));
    try {
      await Promise.all([disconnectUpstream(id), minDelay]);
      reload();
    } finally {
      setBusyAction(null);
    }
  };

  const handleReconnect = async () => {
    setBusyAction("reconnect");
    setConnectError(null);
    onOptimisticReset?.();
    const minDelay = new Promise((r) => setTimeout(r, MIN_DELAY));
    try {
      const [result] = await Promise.all([reconnectUpstream(id), minDelay]);
      if (!startOAuthFlow(id, result, () => reconnectUpstream(id))) return;
      // ``outcome === "pending"`` is the fire-and-forget signal from
      // the service_account branch: the backend has kicked off a
      // detached connect task (typically a 1–60s stdio sandbox cold-
      // pull) and we should stop waiting on the response. The
      // button's visible "Starting…" state hands off to
      // ``UpstreamSummary.starting`` (server truth, surviving tab
      // switch / reload). ``reload()`` pulls in the new value on the
      // next render; the next ``policy_changed`` SSE event (fired
      // after the post-connect catalog refresh) drops ``starting``
      // back to false and flips the button to Stop.
      if (result.outcome === "pending") {
        reload();
      } else if (result.error) {
        setConnectError(result.error);
        onError?.(result.error);
        reload();
      } else {
        reload();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Reconnection failed";
      setConnectError(msg);
      reload();
    }
    setBusyAction(null);
  };

  return {
    busyAction,
    connectError,
    setConnectError,
    handleConnect,
    handleDisconnect,
    handleReconnect,
  };
}
