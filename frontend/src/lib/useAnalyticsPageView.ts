import { useEffect, useRef } from "react";
import { useLocation } from "react-router";
import { track } from "./analytics";

/**
 * Fires a ``page_viewed`` event on every path change. Mount this once
 * per layout so marketing and app surfaces both report, with a
 * ``surface`` tag indicating which one.
 */
export function useAnalyticsPageView(surface: "marketing" | "app", isAuthenticated: boolean): void {
  const { pathname } = useLocation();
  const lastPath = useRef<string | null>(null);

  useEffect(() => {
    if (lastPath.current === pathname) return;
    lastPath.current = pathname;
    track("page_viewed", {
      path: pathname,
      surface,
      is_authenticated: isAuthenticated,
    });
  }, [pathname, surface, isAuthenticated]);
}
