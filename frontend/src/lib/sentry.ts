import * as Sentry from "@sentry/react";

export function initSentry(): void {
  const dsn = import.meta.env.VITE_SENTRY_DSN;
  if (!dsn) return;

  Sentry.init({
    dsn,
    environment: import.meta.env.MODE,
    release: import.meta.env.VITE_RELEASE || undefined,
    integrations: [
      Sentry.browserTracingIntegration(),
    ],
    tracesSampleRate: Number(import.meta.env.VITE_SENTRY_TRACES_SAMPLE_RATE ?? "0.1"),
    tracePropagationTargets: [/^\/api/, /^\/mcp/, /^\/admin-mcp/, /^\/oauth/],
    sendDefaultPii: false,
    // A bare "Not authenticated" 401 is an expected operational event
    // (signing-secret rotation on redeploy, 7-day cookie TTL expiry,
    // logout in another tab), not a code bug. The global session-expiry
    // handler routes it to the sign-in screen; drop it here so any
    // remaining uncaught call site can't spam Sentry with it.
    ignoreErrors: ["Not authenticated"],
  });
}

export function setSentryUser(email: string | null, orgSlug: string | null): void {
  if (!import.meta.env.VITE_SENTRY_DSN) return;
  if (email) {
    Sentry.setUser({ email });
  } else {
    Sentry.setUser(null);
  }
  if (orgSlug) {
    Sentry.setTag("org", orgSlug);
  }
}

export { Sentry };
