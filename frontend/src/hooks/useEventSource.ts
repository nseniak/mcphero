import { useEffect, useRef, useState } from "react";

import { withOrgParam } from "../api/client";

type EventHandler = (data: unknown) => void;

/**
 * Generic hook that connects to the SSE endpoint and dispatches
 * named events to the provided handlers.
 */
export function useEventSource(
  handlers: Record<string, EventHandler>,
  enabled: boolean = true,
): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!enabled) return;
    const es = new EventSource(withOrgParam("/api/events"));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);

    const types = Object.keys(handlersRef.current);
    const listeners: [string, (e: MessageEvent) => void][] = types.map(
      (type) => {
        const listener = (e: MessageEvent) => {
          const data = JSON.parse(e.data);
          handlersRef.current[type]?.(data);
        };
        es.addEventListener(type, listener);
        return [type, listener];
      },
    );

    return () => {
      for (const [type, listener] of listeners) {
        es.removeEventListener(type, listener);
      }
      es.close();
    };
  }, [enabled]);

  return { connected };
}
