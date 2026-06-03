import { useState } from "react";
import { Outlet, useParams, useSearchParams } from "react-router";
import { Menu } from "lucide-react";
import { Sidebar } from "./Sidebar";
import { OrgSwitcher } from "./OrgSwitcher";
import { PlanBadge } from "./PlanBadge";
import { StartupBanner } from "../StartupBanner";
import { useAuth } from "../../hooks/useAuth";
import { useFeatures } from "../../hooks/useFeatures";
import { useTranslation } from "../../i18n/index";
import { getLoginUrl } from "../../api/auth";
import { useAnalyticsPageView } from "../../lib/useAnalyticsPageView";
import { track } from "../../lib/analytics";

export function DashboardLayout() {
  const { user, loading, error, hasUsers, logout } = useAuth();
  const { mode } = useFeatures();
  const { t } = useTranslation();
  const { slug } = useParams<{ slug: string }>();
  const [searchParams] = useSearchParams();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  useAnalyticsPageView("app", !!user);

  const authError = searchParams.get("auth_error");
  const authEmail = searchParams.get("email");
  const viaSuperadmin = searchParams.get("via") === "superadmin";

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen text-zinc-500">
        {t("common.loading")}
      </div>
    );
  }

  // Access denied: not a team member (redirect from OAuth callback)
  if (authError === "not_a_member") {
    return (
      <div className="flex items-center justify-center h-screen bg-zinc-50">
        <div className="text-center space-y-5 max-w-sm">
          <img src="/hero-logo.svg" alt="MCP Hero" className="h-20 w-auto mx-auto" />
          <h1 className="text-xl font-semibold text-zinc-900">Access Denied</h1>
          <p className="text-sm text-zinc-500">
            <span className="font-medium text-zinc-700">{authEmail}</span>{" "}
            is not a member of this team. Ask an admin to add you, then try again.
          </p>
          <a
            href={getLoginUrl()}
            className="inline-block px-4 py-2 text-sm bg-zinc-900 text-white rounded hover:bg-zinc-800 transition-colors"
          >
            Try another account
          </a>
        </div>
      </div>
    );
  }

  // User was removed after signing in (403 from /api/auth/me)
  // Skip this screen when no users exist — fall through to the welcome screen instead.
  if (error && hasUsers) {
    return (
      <div className="flex items-center justify-center h-screen bg-zinc-50">
        <div className="text-center space-y-5 max-w-sm">
          <img src="/hero-logo.svg" alt="MCP Hero" className="h-20 w-auto mx-auto" />
          <h1 className="text-xl font-semibold text-zinc-900">Session Expired</h1>
          <p className="text-sm text-zinc-500">{error}</p>
          <a
            href={getLoginUrl()}
            className="inline-block px-4 py-2 text-sm bg-zinc-900 text-white rounded hover:bg-zinc-800 transition-colors"
          >
            {t("auth.signInGoogle")}
          </a>
        </div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="text-center space-y-4 max-w-lg">
          <img src="/hero-logo.svg" alt="MCP Hero" className="h-24 w-auto mx-auto" />
          <h1 className="text-2xl font-semibold text-zinc-900">
            {hasUsers ? t("auth.title") : t("auth.welcomeTitle")}
          </h1>
          <p className="text-zinc-500">
            {hasUsers ? t("auth.subtitle") : t("auth.welcomeSubtitle")}
          </p>
          {!hasUsers && (
            <p className="text-zinc-500 text-sm">
              {t("auth.welcomeSetup")}
            </p>
          )}
          <a
            href={getLoginUrl()}
            className="inline-block px-4 py-2 bg-zinc-900 text-white rounded hover:bg-zinc-800 transition-colors"
          >
            {t("auth.signInGoogle")}
          </a>
        </div>
      </div>
    );
  }

  // URL has an org slug but the user isn't a member of that org.
  // Superadmins bypass this — they can browse any org by design,
  // drilling into /orgs/:slug/*?via=superadmin from the superadmin tab.
  if (
    slug &&
    mode === "cloud" &&
    !user.is_superadmin &&
    !user.orgs.some((o) => o.slug === slug)
  ) {
    return (
      <div className="flex items-center justify-center h-screen bg-zinc-50">
        <div className="text-center space-y-5 max-w-sm">
          <img src="/hero-logo.svg" alt="MCP Hero" className="h-20 w-auto mx-auto" />
          <h1 className="text-xl font-semibold text-zinc-900">Organization not found</h1>
          <p className="text-sm text-zinc-500">
            You don't have access to <strong>{slug}</strong>, or it doesn't exist.
          </p>
          <a
            href="/"
            className="inline-block px-4 py-2 text-sm bg-zinc-900 text-white rounded hover:bg-zinc-800"
          >
            Go to your dashboard
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-white">
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <main className="flex-1 overflow-auto min-w-0">
        <header className="flex items-center justify-between gap-2 px-3 sm:px-6 py-3 border-b border-zinc-200">
          <div className="flex items-center gap-2 sm:gap-3 min-w-0">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open menu"
              className="md:hidden -ml-1 p-2 text-zinc-600 hover:text-zinc-900 hover:bg-zinc-100 rounded transition-colors"
            >
              <Menu size={20} />
            </button>
            <OrgSwitcher />
            <PlanBadge />
          </div>
          <div className="flex items-center gap-3 text-sm text-zinc-600">
            <span className="hidden sm:inline truncate max-w-[180px]">{user.email}</span>
            {user.is_admin && (
              <span className="hidden sm:inline px-1.5 py-0.5 bg-zinc-200 text-zinc-700 rounded text-xs font-medium">
                {t("auth.adminBadge")}
              </span>
            )}
            <button
              onClick={() => {
                track("user_logged_out", {});
                logout();
              }}
              className="text-zinc-400 hover:text-zinc-600 transition-colors whitespace-nowrap"
            >
              {t("auth.signOut")}
            </button>
          </div>
        </header>
        {user.is_admin && <StartupBanner />}
        {viaSuperadmin && user.is_superadmin && (
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-purple-200 bg-purple-50 px-6 py-2 text-xs text-purple-800">
            <span>
              Viewing as superadmin via{" "}
              <a
                href="/superadmin"
                className="font-medium underline hover:text-purple-900"
              >
                cross-org dashboard
              </a>
              . Actions you take here apply to this org as a normal admin
              would — this banner is informational, not a read-only mode.
            </span>
            {/* Full navigation to /app (not a Link): DefaultRedirect lands
                the user on their OWN org by role, and the reload clears any
                cross-org react-query cache from the org just browsed. */}
            <a
              href="/app"
              className="shrink-0 font-medium underline hover:text-purple-900"
            >
              Exit to my organization →
            </a>
          </div>
        )}
        <div className="p-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
