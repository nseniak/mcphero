import { useState } from "react";
import * as Sentry from "@sentry/react";
import { track, isAnalyticsEnabled } from "../../lib/analytics";
import { useAuth } from "../../hooks/useAuth";

function RenderCrash({ crash }: { crash: boolean }) {
  if (crash) {
    throw new Error("sentry smoke test (frontend render, caught by ErrorBoundary)");
  }
  return null;
}

/**
 * Verifies frontend + backend are wired up to Sentry and Mixpanel.
 *
 * Lives under ``/superadmin/test-observability``. Access is gated at the
 * route level by ``SuperadminGuard``; the page itself does no auth check.
 */
export function TestObservabilityPage() {
  const { user } = useAuth();
  const [crash, setCrash] = useState(false);
  const [lastAction, setLastAction] = useState<string | null>(null);
  // Defaults to the signed-in superadmin so "send to myself" is one click.
  const [testEmail, setTestEmail] = useState(user?.email ?? "");
  const [sendingEmail, setSendingEmail] = useState(false);

  async function triggerBackend() {
    setLastAction("Calling /api/debug/sentry-error…");
    try {
      const res = await fetch("/api/debug/sentry-error");
      setLastAction(`Backend responded ${res.status} (should be 500). Check Sentry.`);
    } catch (e) {
      setLastAction(`Fetch failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  function triggerHandlerThrow() {
    setLastAction("Throwing async — check browser network + Sentry.");
    setTimeout(() => {
      throw new Error("sentry smoke test (frontend setTimeout handler)");
    }, 0);
  }

  function triggerManualCapture() {
    setLastAction("Called Sentry.captureException. Check Sentry.");
    Sentry.captureException(
      new Error("sentry smoke test (frontend manual captureException)"),
    );
  }

  function triggerRenderCrash() {
    setLastAction("Triggering render crash — ErrorBoundary should take over.");
    setCrash(true);
  }

  function triggerFrontendMixpanel() {
    const enabled = isAnalyticsEnabled();
    track("debug_frontend_event", { source: "debug_page" });
    setLastAction(
      enabled
        ? "Fired debug_frontend_event — check Mixpanel."
        : "Mixpanel disabled (VITE_MIXPANEL_TOKEN empty). Event was a no-op.",
    );
  }

  async function triggerTestEmail() {
    const recipient = testEmail.trim();
    if (!recipient.includes("@")) {
      setLastAction("Enter a valid recipient email address first.");
      return;
    }
    setSendingEmail(true);
    setLastAction(`Sending test email to ${recipient}…`);
    try {
      const res = await fetch("/api/debug/test-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: recipient }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setLastAction(
          `Backend responded ${res.status}: ${body?.detail ?? "send failed"}`,
        );
        return;
      }
      setLastAction(
        body.real_delivery
          ? `Sent via ${body.transport} to ${body.to} — check the inbox (and spam).`
          : `Stub transport (${body.transport}) — no real email sent. Set MCPOLIS_SMTP_* and restart to deliver.`,
      );
    } catch (e) {
      setLastAction(`Fetch failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSendingEmail(false);
    }
  }

  async function triggerBackendMixpanel() {
    setLastAction("Calling POST /api/debug/mixpanel-event…");
    try {
      const res = await fetch("/api/debug/mixpanel-event", { method: "POST" });
      if (!res.ok) {
        setLastAction(`Backend responded ${res.status}. Not signed in?`);
        return;
      }
      const body = await res.json();
      setLastAction(
        body.analytics_enabled
          ? "Fired debug_backend_event — check Mixpanel."
          : "Mixpanel disabled (MCPOLIS_MIXPANEL_TOKEN empty). Event was a no-op.",
      );
    } catch (e) {
      setLastAction(`Fetch failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <div className="p-8 max-w-2xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Observability smoke tests</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Use these to verify frontend and backend receive Sentry errors and Mixpanel events.
        </p>
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-zinc-700 uppercase tracking-wider">Sentry</h2>
        <button
          onClick={triggerBackend}
          className="w-full px-4 py-2 bg-zinc-900 text-white rounded text-sm hover:bg-zinc-800 text-left"
        >
          <div className="font-medium">Raise in backend</div>
          <div className="text-xs text-zinc-300 mt-0.5">
            GET /api/debug/sentry-error — captured by backend Sentry project.
          </div>
        </button>

        <button
          onClick={triggerHandlerThrow}
          className="w-full px-4 py-2 bg-white border border-zinc-300 text-zinc-900 rounded text-sm hover:bg-zinc-50 text-left"
        >
          <div className="font-medium">Throw in setTimeout handler</div>
          <div className="text-xs text-zinc-500 mt-0.5">
            Hits window.onerror — captured by frontend Sentry project.
          </div>
        </button>

        <button
          onClick={triggerManualCapture}
          className="w-full px-4 py-2 bg-white border border-zinc-300 text-zinc-900 rounded text-sm hover:bg-zinc-50 text-left"
        >
          <div className="font-medium">Sentry.captureException (manual)</div>
          <div className="text-xs text-zinc-500 mt-0.5">
            Explicit capture — no-op if VITE_SENTRY_DSN is empty.
          </div>
        </button>

        <button
          onClick={triggerRenderCrash}
          className="w-full px-4 py-2 bg-white border border-zinc-300 text-zinc-900 rounded text-sm hover:bg-zinc-50 text-left"
        >
          <div className="font-medium">Crash React render</div>
          <div className="text-xs text-zinc-500 mt-0.5">
            Throws during render — caught by Sentry.ErrorBoundary in main.tsx.
          </div>
        </button>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-zinc-700 uppercase tracking-wider">Mixpanel</h2>
        <button
          onClick={triggerFrontendMixpanel}
          className="w-full px-4 py-2 bg-white border border-zinc-300 text-zinc-900 rounded text-sm hover:bg-zinc-50 text-left"
        >
          <div className="font-medium">Fire frontend event</div>
          <div className="text-xs text-zinc-500 mt-0.5">
            Calls track('debug_frontend_event') — no-op when VITE_MIXPANEL_TOKEN is empty.
          </div>
        </button>

        <button
          onClick={triggerBackendMixpanel}
          className="w-full px-4 py-2 bg-zinc-900 text-white rounded text-sm hover:bg-zinc-800 text-left"
        >
          <div className="font-medium">Fire backend event</div>
          <div className="text-xs text-zinc-300 mt-0.5">
            POST /api/debug/mixpanel-event — fires server-side track, distinct_id = your email.
          </div>
        </button>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-zinc-700 uppercase tracking-wider">Email</h2>
        <input
          type="email"
          value={testEmail}
          onChange={(e) => setTestEmail(e.target.value)}
          placeholder="recipient@example.com"
          className="w-full px-3 py-2 bg-white border border-zinc-300 rounded text-sm text-zinc-900 focus:outline-none focus:ring-2 focus:ring-zinc-400"
        />
        <button
          onClick={triggerTestEmail}
          disabled={sendingEmail}
          className="w-full px-4 py-2 bg-zinc-900 text-white rounded text-sm hover:bg-zinc-800 text-left disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <div className="font-medium">{sendingEmail ? "Sending…" : "Send test email"}</div>
          <div className="text-xs text-zinc-300 mt-0.5">
            POST /api/debug/test-email — sends through the wired EmailSender (SMTP in prod, logging stub otherwise).
          </div>
        </button>
      </section>

      {lastAction && (
        <div className="p-3 bg-zinc-50 border border-zinc-200 rounded text-sm text-zinc-700">
          {lastAction}
        </div>
      )}

      <RenderCrash crash={crash} />
    </div>
  );
}
