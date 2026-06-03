import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router";
import { checkSlug, createOrg, switchOrg } from "../api/orgs";
import { FieldHint } from "../components/ui/field-hint";
import { useAuth } from "../hooks/useAuth";
import { track } from "../lib/analytics";
import { reportClientError } from "../lib/clientErrorReporter";

function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
}

const SLUG_REGEX = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/;
const SLUG_CHARS = /[^a-z0-9-]/g;

function validateSlug(value: string): string | null {
  if (!value) return null; // don't show error on empty
  if (SLUG_CHARS.test(value)) return "Only lowercase letters, numbers, and hyphens allowed";
  if (value.startsWith("-") || value.endsWith("-")) return "Cannot start or end with a hyphen";
  if (value.length < 3) return "Must be at least 3 characters";
  if (value.length > 40) return "Must be at most 40 characters";
  if (!SLUG_REGEX.test(value)) return "Invalid format";
  return null;
}

export function SignupPage() {
  const { user, loading: authLoading } = useAuth();
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  // Two error sources, two pieces of state:
  //
  // * ``slugLocalError`` — fails ``validateSlug`` (length, charset, ...).
  //   Suppressed until the user has interacted with the slug field
  //   (``slugTouched``) so we don't yell at them while they're still
  //   typing the org name and the auto-suggester is mid-update.
  // * ``slugServerError`` — the backend's ``check-slug`` returned
  //   ``available: false`` (taken / reserved) or the create POST
  //   itself was rejected. *Always* visible — the user genuinely
  //   needs to know, regardless of which field they last clicked.
  //
  // The original code collapsed both into one ``slugHint`` and gated
  // its rendering on ``slugTouched``. That hid taken-slug warnings
  // for any user who only typed the name field — and worse, the
  // submit handler kept a separate guard that aborted on the hidden
  // hint, producing a "click does nothing" UX (the athena-decision
  // bug). Splitting the two states makes each one's render rule and
  // each one's submit-block rule symmetric by construction.
  const [slugLocalError, setSlugLocalError] = useState<string | null>(null);
  const [slugServerError, setSlugServerError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(null);

  const checkSlugAvailability = useCallback((value: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const localError = validateSlug(value);
    setSlugLocalError(localError);
    // Any keystroke makes a previously-cached server verdict stale.
    setSlugServerError(null);
    if (localError || !value) return;
    debounceRef.current = setTimeout(async () => {
      try {
        const result = await checkSlug(value);
        if (!result.available) {
          setSlugServerError(result.reason);
        }
      } catch {
        // Ignore network errors during validation
      }
    }, 400);
  }, []);

  useEffect(() => {
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, []);

  // Fire signup_started once when the form becomes interactive.
  useEffect(() => {
    track("signup_started", {});
  }, []);

  function onNameChange(value: string) {
    setDisplayName(value);
    if (!slugEdited) {
      const suggested = suggestSlug(value);
      setSlug(suggested);
      checkSlugAvailability(suggested);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSlugTouched(true);
    // Re-validate before submitting. The button's disabled rule
    // already mirrors these guards, so the only way through to a
    // silent return here is a programmatic submit (Enter inside an
    // input, ``form.requestSubmit()``, etc.). Telemetry covers that
    // edge case so a future regression shows up in Discover.
    const localError = validateSlug(slug);
    if (localError) {
      setSlugLocalError(localError);
      track("signup_blocked", { reason: "local_validation", slug, hint: localError });
      reportClientError({
        event: "client.reported_error",
        message: "signup.blocked",
        context: { reason: "local_validation", slug, hint: localError },
      });
      return;
    }
    if (slugServerError) {
      track("signup_blocked", { reason: "slug_unavailable", slug, hint: slugServerError });
      reportClientError({
        event: "client.reported_error",
        message: "signup.blocked",
        context: { reason: "slug_unavailable", slug, hint: slugServerError },
      });
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const org = await createOrg(slug, displayName, "signup");
      await switchOrg(org.slug);
      track("signup_completed", { org_slug: org.slug });
      // Force full reload so cookies + auth context pick up the new org
      window.location.href = "/";
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to create organization";
      // Slug-related backend rejections belong next to the slug
      // field — and route into ``slugServerError`` so they're
      // visible regardless of focus.
      if (msg.toLowerCase().includes("short name") || msg.toLowerCase().includes("taken") || msg.toLowerCase().includes("reserved")) {
        setSlugServerError(msg);
      } else {
        setError(msg);
      }
      setSubmitting(false);
    }
  }

  // Guard: /signup only makes sense for an authenticated user who
  // doesn't yet have an org. Anyone else is redirected:
  //   * not signed in → /home (marketing front door)
  //   * signed in with an org → /app (the DefaultRedirect picks the
  //     right landing — admin upstream / connect / my-tools)
  if (authLoading) return null;
  if (!user) return <Navigate to="/home" replace />;
  if (user.current_org) return <Navigate to="/app" replace />;

  // The gateway may live on a subdomain (``mcp.mcphero.io``) while the
  // dashboard stays on apex. Prefer the explicit build-time
  // ``VITE_GATEWAY_URL`` so the preview shown to a signing-up user
  // matches what they'll actually paste into Claude. Fall back to
  // ``window.location.origin`` when unset (single-origin deploys).
  const gatewayOrigin =
    (import.meta.env.VITE_GATEWAY_URL as string | undefined)?.replace(/\/+$/, "") ??
    (typeof window !== "undefined" ? window.location.origin : "");
  const previewSlug = slug.trim() || "acme-corp";

  return (
    <section className="px-6 pt-16 pb-20 md:pt-20 md:pb-24">
      <div className="max-w-xl mx-auto space-y-8">
        <div className="text-center space-y-3">
          <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">
            One last step
          </p>
          <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 tracking-tight">
            Create your organization
          </h1>
          <p className="text-zinc-600 text-base md:text-lg max-w-md mx-auto">
            Organizations let you manage MCP servers, users, and permissions in one place.
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-xl border border-zinc-200 bg-white shadow-sm p-6 md:p-8 space-y-5"
        >
          <div>
            <label htmlFor="display-name" className="block text-sm font-medium text-zinc-700 mb-1">
              Organization name
            </label>
            <input
              id="display-name"
              type="text"
              value={displayName}
              onChange={(e) => onNameChange(e.target.value)}
              placeholder="Acme Corp"
              required
              className="w-full px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900 focus:border-transparent"
            />
          </div>

          <div>
            <label htmlFor="slug" className="block text-sm font-medium text-zinc-700 mb-1">
              Short name <span className="font-normal text-zinc-500">(unique across MCP Hero)</span>
            </label>
            <input
              id="slug"
              type="text"
              value={slug}
              onFocus={() => setSlugTouched(true)}
              onChange={(e) => {
                const raw = e.target.value.toLowerCase().replace(SLUG_CHARS, "");
                setSlug(raw);
                setSlugEdited(true);
                checkSlugAvailability(raw);
              }}
              placeholder="acme-corp"
              required
              className="w-full px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900 focus:border-transparent"
            />
            {/*
              Render order:
                1. Server-side error → always visible (taken / reserved
                   names must be obvious even if the user only typed
                   the org-name field — the bug we just fixed).
                2. Local validation error → only after the user has
                   focused the slug field, so the auto-suggester's
                   transient "too short" state doesn't shout at them
                   while they're still typing the org name.
                3. Otherwise: helper text.
            */}
            {/* Marketing-page tone: most messages stay neutral (the
                wording carries the meaning). The exception is the
                server's "already taken / reserved" verdict — that one
                requires the user to pick a different name and earns
                red so it's unmissable. Local format errors and the
                generic submit error stay quiet. */}
            {slugServerError ? (
              <FieldHint error>{slugServerError}</FieldHint>
            ) : slugTouched && slugLocalError ? (
              <FieldHint>{slugLocalError}</FieldHint>
            ) : (
              <FieldHint>Lowercase letters, numbers, and hyphens.</FieldHint>
            )}
          </div>

          <div>
            <label className="block text-sm font-medium text-zinc-700 mb-1">
              URL of your shared MCP
            </label>
            <code className="block w-full px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-md text-sm font-mono text-zinc-900 break-all select-all">
              {gatewayOrigin}/mcp/
              <span className={slug.trim() ? "text-zinc-900" : "text-zinc-400"}>
                {previewSlug}
              </span>
            </code>
            <p className="mt-1 text-xs text-zinc-500">
              Your team will paste this into Claude, ChatGPT, Cursor, or any MCP client to reach
              every MCP you add to your organization.
            </p>
          </div>

          {error && <FieldHint>{error}</FieldHint>}

          <button
            type="submit"
            disabled={submitting || !displayName.trim() || !slug.trim() || !!slugServerError || (slugTouched && !!slugLocalError)}
            className="w-full px-4 py-2.5 bg-zinc-900 text-white rounded-md text-sm font-medium hover:bg-zinc-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {submitting ? "Creating..." : "Create organization"}
          </button>
        </form>
      </div>
    </section>
  );
}
