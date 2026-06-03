/**
 * Client-side error / event reporter.
 *
 * Posts structured payloads to ``POST /api/client-errors`` so they
 * land in the same Vector → Elastic pipeline as backend logs. Sentry
 * already captures unhandled exceptions for triage; this channel
 * exists so ops can grep frontend records *alongside* backend
 * structlog records on the same timeline (e.g. "what did the user
 * do in the 30s before the backend error fired").
 *
 * Use cases:
 *
 * * ``installClientErrorReporter()`` once at boot — catches
 *   ``window.onerror`` and ``unhandledrejection``.
 * * ``reportClientError({ event: "client.reported_error", ... })``
 *   for silent product-flow paths (e.g. a form-submit handler that
 *   early-returns) that Sentry would otherwise never see.
 *
 * Failures (network down, 4xx, etc.) are swallowed — the reporter
 * MUST NOT itself throw, or it will mask the original error.
 */

const RELEASE = (import.meta.env.VITE_RELEASE as string | undefined) ?? "";
const APP_ENV = import.meta.env.MODE;

export type ClientErrorEvent =
  | "client.unhandled_error"
  | "client.unhandled_rejection"
  | "client.reported_error";

export interface ClientErrorReport {
  event: ClientErrorEvent;
  message?: string;
  stack?: string;
  url?: string;
  source?: string;
  line?: number;
  column?: number;
  /** Free-form properties merged into ``message`` as JSON. The backend
   *  logs the message verbatim; structured fields belong here so
   *  Discover can facet on them. */
  context?: Record<string, unknown>;
}

const MAX_FIELD_LEN = 1000;

function clip(value: string | undefined, limit = MAX_FIELD_LEN): string | undefined {
  if (value === undefined) return undefined;
  if (value.length <= limit) return value;
  return value.slice(0, limit) + "...[clipped]";
}

export function reportClientError(report: ClientErrorReport): void {
  const message = report.context !== undefined
    ? `${report.message ?? ""} ${JSON.stringify(report.context)}`.trim()
    : report.message;
  const body = JSON.stringify({
    event: report.event,
    message: clip(message),
    stack: clip(report.stack, MAX_FIELD_LEN * 4),
    url: clip(report.url ?? (typeof window !== "undefined" ? window.location.href : undefined)),
    source: clip(report.source),
    line: report.line,
    column: report.column,
    release: RELEASE || undefined,
    app_environment: APP_ENV,
  });
  // ``keepalive`` lets the request survive a page unload (e.g. user
  // navigated away after the silent-abort). Ignore the response.
  try {
    void fetch("/api/client-errors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      keepalive: true,
      body,
    }).catch(() => undefined);
  } catch {
    // Reporter MUST NOT throw — never mask the original error.
  }
}

let installed = false;

export function installClientErrorReporter(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;

  window.addEventListener("error", (event) => {
    reportClientError({
      event: "client.unhandled_error",
      message: event.message,
      stack: event.error instanceof Error ? event.error.stack : undefined,
      source: event.filename,
      line: event.lineno,
      column: event.colno,
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    const reason: unknown = event.reason;
    const message = reason instanceof Error
      ? reason.message
      : typeof reason === "string"
        ? reason
        : JSON.stringify(reason);
    reportClientError({
      event: "client.unhandled_rejection",
      message,
      stack: reason instanceof Error ? reason.stack : undefined,
    });
  });
}
