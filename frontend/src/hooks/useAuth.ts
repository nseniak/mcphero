import { createContext, useContext, useEffect, useRef, useState } from "react";
import { fetchMe, fetchAuthStatus, logout as apiLogout } from "../api/auth";
import { setSentryUser } from "../lib/sentry";
import { identify as analyticsIdentify, reset as analyticsReset } from "../lib/analytics";
import { setSessionExpiredHandler } from "../lib/session";
import type { UserInfo } from "../api/types";

// Shown on the existing "Session Expired" screen when a live session
// dies mid-use (see DashboardLayout's ``error && hasUsers`` branch).
const SESSION_EXPIRED_MESSAGE =
  "Your session expired. Please sign in again.";

interface AuthState {
  user: UserInfo | null;
  loading: boolean;
  error: string | null;
  hasUsers: boolean;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  error: null,
  hasUsers: true,
  logout: async () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

export function useAuthProvider(): AuthState {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasUsers, setHasUsers] = useState(true);

  // Mirror "are we currently authenticated?" into a ref so the
  // session-expiry handler (registered once) can read the live value
  // without re-subscribing on every auth change.
  const authedRef = useRef(false);
  useEffect(() => {
    authedRef.current = user !== null;
  }, [user]);

  // Register the global mid-session 401 handler. ``apiFetch`` fires it
  // on every 401; we only act once a user is live (a 401 during the
  // initial probe is just "not signed in", handled below). Clearing the
  // user + setting ``error`` routes DashboardLayout to its existing
  // "Session Expired" screen. ``authedRef`` is flipped false up front so
  // the burst of 401s a dead session produces collapses into one flip.
  useEffect(() => {
    setSessionExpiredHandler(() => {
      if (!authedRef.current) return;
      authedRef.current = false;
      setUser(null);
      setError(SESSION_EXPIRED_MESSAGE);
      setSentryUser(null, null);
      analyticsReset();
    });
    return () => setSessionExpiredHandler(null);
  }, []);

  useEffect(() => {
    Promise.all([
      fetchMe().catch((e) => {
        // 401 = not signed in (normal). 429 = rate limited (transient,
        // retry on next page load — don't show "Session Expired").
        if (e.status !== 401 && e.status !== 429) setError(e.message);
        return null;
      }),
      fetchAuthStatus().catch(() => ({ has_users: true })),
    ]).then(([userData, status]) => {
      if (userData) {
        setUser(userData);
        setSentryUser(userData.email, userData.current_org?.slug ?? null);
        analyticsIdentify(userData.email, {
          org_id: userData.current_org?.slug ?? null,
          org_role: userData.current_org?.role ?? null,
          is_admin: userData.is_admin,
        });
      }
      setHasUsers(status.has_users);
      setLoading(false);
    });
  }, []);

  const logout = async () => {
    await apiLogout();
    setUser(null);
    setSentryUser(null, null);
    analyticsReset();
  };

  return { user, loading, error, hasUsers, logout };
}
