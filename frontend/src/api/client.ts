/** Base API client. All requests include credentials (cookie auth). */

import { notifySessionExpired } from "../lib/session";

export class ApiError extends Error {
  status: number;
  /** Raw structured detail when the response body was JSON with a
   *  non-string ``detail`` (e.g. the sandbox-resources off-grid 400
   *  returns ``{detail: {message, field, value}}``). Callers that
   *  need the structured fields read this; callers that want a
   *  human-readable string read ``message``. */
  detail?: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

/** Subclass surfaced when the backend returns the documented 402
 *  ``plan_limit_exceeded`` envelope. Mutation handlers throughout the
 *  app catch this with ``instanceof PlanLimitError`` and route to the
 *  shared upgrade modal via ``handlePlanLimitError``. */
export class PlanLimitError extends ApiError {
  gate: string;
  current: number | null;
  limit: number | null;
  constructor(
    message: string,
    gate: string,
    current: number | null,
    limit: number | null,
  ) {
    super(402, message);
    this.gate = gate;
    this.current = current;
    this.limit = limit;
  }
}

/**
 * Org slug taken from the current `/orgs/{slug}/...` route, or `null`
 * on slug-less routes (own / default org) where the session cookie is
 * authoritative.
 *
 * Read straight off `window.location.pathname` so it's always current
 * at request time, with no React wiring to race the queries a page
 * fires on mount. Only super-admins viewing another org actually need
 * this — the backend honours it solely for them (see
 * `OrgContextMiddleware`); for everyone else the route slug equals
 * their cookie org, so sending it changes nothing.
 */
export function currentRouteOrgSlug(): string | null {
  const m = window.location.pathname.match(/^\/orgs\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Append `?org=<slug>` to a URL for the current route's org. Used by
 * EventSource, which can't set the `X-Org-Slug` request header
 * `apiFetch` uses. No-op on slug-less routes.
 */
export function withOrgParam(url: string): string {
  const slug = currentRouteOrgSlug();
  if (!slug) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}org=${encodeURIComponent(slug)}`;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const orgSlug = currentRouteOrgSlug();
  const resp = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(orgSlug ? { "X-Org-Slug": orgSlug } : {}),
      ...init?.headers,
    },
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => resp.statusText);
    let message = text;
    let detail: unknown;
    try {
      const json = JSON.parse(text);
      if (json.detail !== undefined && json.detail !== null) {
        // FastAPI puts string detail (``raise HTTPException(400, "msg")``)
        // and structured detail (``raise HTTPException(400, detail={...})``)
        // into the same key. Stringified JS coercion of an object is
        // ``"[object Object]"`` — which is what users saw before. So
        // surface the human-readable ``.message`` (when the structured
        // detail carries one) as the Error message, and stash the
        // whole object on ``ApiError.detail`` for callers that need
        // ``field`` / ``value``.
        if (typeof json.detail === "string") {
          message = json.detail;
        } else if (typeof json.detail === "object") {
          const det = json.detail as Record<string, unknown>;
          message = typeof det.message === "string"
            ? det.message
            : JSON.stringify(det);
          detail = det;
        } else {
          message = String(json.detail);
        }
      } else if (json.error === "rate_limited") {
        message = `Too many requests. Please wait ${json.retry_after ?? "a few"} seconds.`;
      } else if (
        resp.status === 402 && json.error === "plan_limit_exceeded"
      ) {
        // Documented envelope from the FastAPI exception handler:
        // ``{error, gate, current, limit, message}``. Surface as a
        // typed subclass so mutation handlers can ``instanceof`` it
        // and open the upgrade modal instead of showing a raw error.
        throw new PlanLimitError(
          typeof json.message === "string" ? json.message : "Upgrade required",
          typeof json.gate === "string" ? json.gate : "unknown",
          typeof json.current === "number" ? json.current : null,
          typeof json.limit === "number" ? json.limit : null,
        );
      } else if (json.error) {
        message = json.error;
      }
    } catch (e) {
      // PlanLimitError is intentionally rethrown — only swallow
      // genuine JSON parse failures so we still throw the generic
      // ApiError with the raw text below.
      if (e instanceof PlanLimitError) {
        throw e;
      }
      // not JSON, use raw text
    }
    if (resp.status === 401) {
      // A 401 from an authenticated call means the live session died
      // (signing-secret rotation on a redeploy, the 7-day cookie TTL
      // expiring, or a logout elsewhere). Signal the auth provider so
      // it can route to the sign-in screen. It ignores this before the
      // user has authenticated, where a 401 just means "not signed in".
      notifySessionExpired();
    }
    throw new ApiError(resp.status, message, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}
