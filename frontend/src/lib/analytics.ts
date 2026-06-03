import mixpanel from "mixpanel-browser";

/**
 * Env-driven Mixpanel wrapper. An empty ``VITE_MIXPANEL_TOKEN`` disables
 * the SDK entirely so dev installs never make network calls. The exported
 * helpers are all safe to call before/after login, and before/after the
 * SDK is initialized — they no-op when disabled.
 */

const TOKEN = import.meta.env.VITE_MIXPANEL_TOKEN as string | undefined;
// mixpanel-browser expects api_host as a full URL (with protocol). Accept
// either form in the env (with or without https://) so the same value
// works whether copied from the Mixpanel docs or passed verbatim from
// the matching backend MCPOLIS_MIXPANEL_API_HOST env (which is just a
// hostname for the Python SDK).
const RAW_API_HOST =
  (import.meta.env.VITE_MIXPANEL_API_HOST as string | undefined) ??
  "https://api.mixpanel.com";
const API_HOST = /^https?:\/\//.test(RAW_API_HOST)
  ? RAW_API_HOST
  : `https://${RAW_API_HOST}`;
const RELEASE = (import.meta.env.VITE_RELEASE as string | undefined) ?? "";
const APP_ENV = import.meta.env.MODE;

let enabled = false;

export function initAnalytics(): void {
  if (!TOKEN) return;
  mixpanel.init(TOKEN, {
    // Don't auto-collect pageviews — we fire page_viewed explicitly on
    // route changes via useAnalyticsPageView, so Mixpanel's own tracking
    // would double-count.
    track_pageview: false,
    persistence: "localStorage",
    ignore_dnt: false,
    // Route to EU endpoint when VITE_MIXPANEL_API_HOST is set to an EU
    // ingestion URL; default is US (api.mixpanel.com).
    api_host: API_HOST,
  });
  enabled = true;
  // Register client-side super-properties that should attach to every event.
  mixpanel.register({
    release: RELEASE || undefined,
    app_environment: APP_ENV,
  });
}

export function identify(email: string, superProperties: Record<string, unknown> = {}): void {
  if (!enabled) return;
  mixpanel.identify(email);
  mixpanel.people.set({ $email: email, ...superProperties });
  if (Object.keys(superProperties).length > 0) {
    mixpanel.register(superProperties);
  }
}

export function reset(): void {
  if (!enabled) return;
  mixpanel.reset();
  // Re-register static super-properties after reset (reset clears them).
  mixpanel.register({
    release: RELEASE || undefined,
    app_environment: APP_ENV,
  });
}

export function registerSuperProperties(props: Record<string, unknown>): void {
  if (!enabled) return;
  mixpanel.register(props);
}

export function track(event: string, properties: Record<string, unknown> = {}): void {
  if (!enabled) return;
  mixpanel.track(event, properties);
}

export function isAnalyticsEnabled(): boolean {
  return enabled;
}
