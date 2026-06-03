import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router";
import { getLoginUrl } from "../api/auth";
import { fetchPublicOrgInfo } from "../api/orgs";

/**
 * Landing page for invite links: ``/orgs/{slug}/join``.
 *
 * Lighter shell than SignupPage — invited end users aren't shopping
 * for the product, so the marketing TopNav (Pricing / Docs / Sign in)
 * would compete with the page's own CTA. Just the brand mark up top
 * and a one-line legal hygiene at the bottom.
 *
 * On mount we fetch ``/api/orgs/{slug}/public`` so the heading can
 * show the org's display name ("Join Acme") instead of its slug
 * ("Join acme-corp"), and so we can render an "invite link looks
 * broken" state when the slug doesn't resolve.
 *
 * After Google sign-in, the backend's callback flow auto-accepts a
 * matching invite and lands the user in the org. If the user signs
 * in but isn't a member, the backend redirects back here with
 * ``?auth_error=not_a_member`` and a softer message (zinc-700, not
 * red — same tone choice as SignupPage).
 */
export function JoinPage() {
  const { slug = "" } = useParams<{ slug: string }>();
  const [searchParams] = useSearchParams();
  const authError = searchParams.get("auth_error");
  const errorEmail = searchParams.get("email");

  const { data: publicInfo, isLoading } = useQuery({
    queryKey: ["org-public", slug],
    queryFn: () => fetchPublicOrgInfo(slug),
    enabled: Boolean(slug),
  });

  const orgLabel = publicInfo?.display_name ?? slug;
  const isInvalidLink = publicInfo !== undefined && !publicInfo.exists;

  return (
    <div className="min-h-screen flex flex-col bg-white">
      <header className="px-6 py-4 border-b border-zinc-200">
        <div className="max-w-6xl mx-auto">
          <Link to="/home" className="inline-flex items-center gap-2">
            <img src="/hero-logo.svg" alt="MCP Hero" className="h-7 w-auto" />
            <span className="text-sm font-semibold text-zinc-900">MCP Hero</span>
          </Link>
        </div>
      </header>

      <main className="flex-1 flex items-center justify-center px-6 py-12">
        <JoinPanel
          slug={slug}
          orgLabel={orgLabel}
          isLoading={isLoading}
          isInvalidLink={isInvalidLink}
          authError={authError}
          errorEmail={errorEmail}
        />
      </main>

      <footer className="px-6 py-4 text-xs text-zinc-500 text-center">
        Copyright © 2026 Nitsan Seniak.
      </footer>
    </div>
  );
}

interface JoinPanelProps {
  slug: string;
  orgLabel: string;
  isLoading: boolean;
  isInvalidLink: boolean;
  authError: string | null;
  errorEmail: string | null;
}

function JoinPanel({
  slug,
  orgLabel,
  isLoading,
  isInvalidLink,
  authError,
  errorEmail,
}: JoinPanelProps) {
  // Don't paint the heading until we know whether the slug resolves.
  // Otherwise the page renders "Join acme-corp" for ~50ms and then
  // jumps to "Join Acme" once the public-info fetch lands.
  if (isLoading) {
    return (
      <div className="w-full max-w-md text-center">
        <div className="h-10 mx-auto w-2/3 rounded bg-zinc-100 animate-pulse" />
        <div className="mt-4 h-4 mx-auto w-3/4 rounded bg-zinc-100 animate-pulse" />
      </div>
    );
  }

  if (isInvalidLink) {
    return (
      <div className="w-full max-w-md text-center space-y-5">
        <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">
          Invite link
        </p>
        <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 tracking-tight">
          This link looks broken
        </h1>
        <p className="text-zinc-600">
          We couldn't find an organization at this URL. Ask whoever sent you
          the invite to share the link again — they may have copied it
          incorrectly, or the team may no longer exist.
        </p>
        <Link
          to="/home"
          className="inline-block px-4 py-2 bg-zinc-900 text-white rounded-md text-sm font-medium hover:bg-zinc-800 transition-colors"
        >
          Go to MCP Hero
        </Link>
      </div>
    );
  }

  return (
    <div className="w-full max-w-md text-center space-y-6">
      <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">
        You've been invited
      </p>
      <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 tracking-tight">
        Join <span className="text-zinc-700">{orgLabel}</span>
      </h1>
      <p className="text-zinc-600 text-base md:text-lg">
        Your team uses MCP Hero to share MCP tools across Claude, ChatGPT,
        Cursor, and other AI clients. Sign in to get the same access.
      </p>

      <div className="rounded-xl border border-zinc-200 bg-white shadow-sm p-6 space-y-4">
        {authError === "not_a_member" ? (
          <>
            <p className="text-sm text-zinc-700">
              {errorEmail ? (
                <>
                  <strong className="font-medium">{errorEmail}</strong> isn't
                  on the <strong className="font-medium">{orgLabel}</strong>{" "}
                  team yet.
                </>
              ) : (
                <>
                  That account isn't on the{" "}
                  <strong className="font-medium">{orgLabel}</strong> team
                  yet.
                </>
              )}
            </p>
            <p className="text-sm text-zinc-500">
              Ask whoever sent you the invite to add this email to the team,
              or sign in with a different Google account.
            </p>
            <a
              href={`${getLoginUrl()}?join=${slug}`}
              className="inline-block w-full px-4 py-2.5 bg-zinc-900 text-white rounded-md text-sm font-medium hover:bg-zinc-800 transition-colors"
            >
              Try a different account
            </a>
          </>
        ) : (
          <a
            href={`${getLoginUrl()}?join=${slug}`}
            className="inline-block w-full px-4 py-2.5 bg-zinc-900 text-white rounded-md text-sm font-medium hover:bg-zinc-800 transition-colors"
          >
            Sign in with Google to join {orgLabel}
          </a>
        )}
      </div>
    </div>
  );
}
