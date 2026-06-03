import { useEffect, useRef, useState } from "react";
import { useEventSource } from "./useEventSource";

interface ConnectResponse {
  connected: boolean;
  authorization_url?: string | null;
  error?: string | null;
}

interface UseOAuthPopupOptions {
  /** Called when connection succeeds (after SSE + finalize call) */
  onSuccess: () => void;
  /** Called on error */
  onError: (error: string) => void;
}

interface UseOAuthPopupResult {
  /** Whether an OAuth flow is in progress */
  busy: boolean;
  /**
   * Start an OAuth flow. Pass the result of the initial connect API call,
   * the upstream ID for SSE filtering, and a function to finalize the
   * connection after tokens are acquired.
   * Returns true if the result was handled synchronously (no popup needed).
   */
  startOAuthFlow: (
    upstreamId: string,
    result: ConnectResponse,
    finalizeConnect: () => Promise<ConnectResponse>,
  ) => boolean;
}

/**
 * Manages an OAuth popup lifecycle: opens popup, listens for SSE events
 * (success or error), detects popup closure, and applies a timeout fallback.
 */
export function useOAuthPopup({
  onSuccess,
  onError,
}: UseOAuthPopupOptions): UseOAuthPopupResult {
  const [waiting, setWaiting] = useState(false);
  const authWindowRef = useRef<Window | null>(null);
  const waitingRef = useRef(false);
  const upstreamIdRef = useRef<string>("");
  const finalizeRef = useRef<(() => Promise<ConnectResponse>) | null>(null);
  const onSuccessRef = useRef(onSuccess);
  const onErrorRef = useRef(onError);
  onSuccessRef.current = onSuccess;
  onErrorRef.current = onError;

  // Listen for SSE events during OAuth flow
  useEventSource(
    {
      upstream_tokens_acquired: (data: unknown) => {
        const payload = data as { payload?: { upstream_id?: string } };
        if (payload?.payload?.upstream_id !== upstreamIdRef.current) return;
        if (!waitingRef.current) return;

        waitingRef.current = false;
        setWaiting(false);
        authWindowRef.current?.close();
        authWindowRef.current = null;

        // Tokens acquired — finalize the connection
        const finalize = finalizeRef.current;
        if (finalize) {
          finalize()
            .then((result) => {
              if (result.connected) {
                onSuccessRef.current();
              } else if (result.error) {
                onErrorRef.current(result.error);
              }
            })
            .catch((e) => {
              onErrorRef.current(e instanceof Error ? e.message : "Connection failed");
            });
        }
      },
      upstream_oauth_error: (data: unknown) => {
        const payload = data as { payload?: { upstream_id?: string; error?: string } };
        if (payload?.payload?.upstream_id !== upstreamIdRef.current) return;
        if (!waitingRef.current) return;

        waitingRef.current = false;
        setWaiting(false);
        authWindowRef.current = null;
        onErrorRef.current(payload?.payload?.error ?? "Authentication failed");
      },
    },
    waiting,
  );

  // Detect popup closed + absolute timeout
  useEffect(() => {
    if (!waiting) return;

    const reset = () => {
      waitingRef.current = false;
      setWaiting(false);
      authWindowRef.current?.close();
      authWindowRef.current = null;

      // Try to finalize — tokens may have been acquired before the popup closed
      const finalize = finalizeRef.current;
      if (finalize) {
        finalize()
          .then((result) => {
            if (result.connected) {
              onSuccessRef.current();
            } else {
              onSuccessRef.current(); // Refresh anyway to pick up state
            }
          })
          .catch(() => {
            onSuccessRef.current();
          });
      } else {
        onSuccessRef.current();
      }
    };

    const check = setInterval(() => {
      if (authWindowRef.current?.closed) {
        clearInterval(check);
        // Grace period — SSE event may arrive just after popup closes
        setTimeout(() => {
          if (waitingRef.current) reset();
        }, 2000);
      }
    }, 500);

    const timeout = setTimeout(() => {
      if (waitingRef.current) reset();
    }, 300000);

    return () => { clearInterval(check); clearTimeout(timeout); };
  }, [waiting]);

  const startOAuthFlow = (
    upstreamId: string,
    result: ConnectResponse,
    finalizeConnect: () => Promise<ConnectResponse>,
  ): boolean => {
    if (result.authorization_url) {
      upstreamIdRef.current = upstreamId;
      finalizeRef.current = finalizeConnect;
      const authWindow = window.open("about:blank", "_blank", "width=600,height=700");
      authWindowRef.current = authWindow;
      if (authWindow) {
        authWindow.location.href = result.authorization_url;
      }
      waitingRef.current = true;
      setWaiting(true);
      return false; // async — waiting for SSE
    }
    // No popup needed — already connected or error
    authWindowRef.current = null;
    return true; // sync — caller handles the result
  };

  return { busy: waiting, startOAuthFlow };
}
