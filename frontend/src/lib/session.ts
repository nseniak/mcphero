/** Global mid-session expiry signal.
 *
 * ``apiFetch`` reports every 401 here; the auth provider registers the
 * handler and decides what a 401 means. It ignores 401s seen before the
 * user ever authenticated (those are just "not signed in", handled by
 * the initial ``/api/auth/me`` probe) and, once a user is live, treats a
 * 401 as the session having died underneath them — secret rotation on a
 * redeploy, the 7-day cookie TTL expiring, or a logout in another tab.
 *
 * Kept out of React so the lowest-level fetch wrapper can fire it without
 * importing hooks or context (which would create an import cycle). */
type SessionExpiredHandler = () => void;

let handler: SessionExpiredHandler | null = null;

export function setSessionExpiredHandler(fn: SessionExpiredHandler | null): void {
  handler = fn;
}

export function notifySessionExpired(): void {
  handler?.();
}
