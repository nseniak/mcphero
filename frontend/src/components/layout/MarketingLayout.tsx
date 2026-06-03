import { Link, NavLink, Outlet } from "react-router";
import { useAuth } from "../../hooks/useAuth";
import { getLoginUrl } from "../../api/auth";
import { useTranslation } from "../../i18n/index";
import { track } from "../../lib/analytics";
import { useAnalyticsPageView } from "../../lib/useAnalyticsPageView";

function TopNav() {
  const { user, logout } = useAuth();
  const { t } = useTranslation();
  return (
    <header className="sticky top-0 z-30 bg-white/80 backdrop-blur border-b border-zinc-200">
      <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
        {/* Logo links to /home (the unconditional marketing render via
            forceShow), not /. Signed-in users sitting on /signup —
            i.e. with no org yet — would otherwise hit a redirect
            loop: / → HomePage → Navigate to /app → DefaultRedirect →
            Navigate back to /signup. /home short-circuits the loop
            and lets the user reach the marketing page they expected. */}
        <Link to="/home" className="flex items-center gap-2">
          <img src="/hero-logo.svg" alt="MCP Hero" className="h-7 w-auto" />
          <span className="text-sm font-semibold text-zinc-900">MCP Hero</span>
        </Link>
        <nav className="hidden md:flex items-center gap-6 text-sm text-zinc-600">
          <NavLink
            to="/pricing"
            className={({ isActive }) =>
              isActive ? "text-zinc-900 font-medium" : "hover:text-zinc-900 transition-colors"
            }
          >
            Pricing
          </NavLink>
          <NavLink
            to="/docs"
            className={({ isActive }) =>
              isActive ? "text-zinc-900 font-medium" : "hover:text-zinc-900 transition-colors"
            }
          >
            Docs
          </NavLink>
        </nav>
        <div className="flex items-center gap-3">
          {user ? (
            <>
              <span className="hidden sm:inline text-sm text-zinc-600">
                {user.email}
              </span>
              <Link
                to="/app"
                className="px-3 py-1.5 rounded-md bg-zinc-900 text-white text-sm font-medium hover:bg-zinc-800 transition-colors"
              >
                Go to app
              </Link>
              <button
                type="button"
                onClick={() => {
                  track("user_logged_out", {});
                  logout();
                }}
                className="text-sm text-zinc-400 hover:text-zinc-600 transition-colors"
              >
                {t("auth.signOut")}
              </button>
            </>
          ) : (
            <>
              <a
                href={getLoginUrl()}
                className="text-sm text-zinc-600 hover:text-zinc-900 transition-colors"
              >
                Sign in
              </a>
              <a
                href={getLoginUrl()}
                className="px-3 py-1.5 rounded-md bg-zinc-900 text-white text-sm font-medium hover:bg-zinc-800 transition-colors"
              >
                Get started
              </a>
            </>
          )}
        </div>
      </div>
    </header>
  );
}

function Footer() {
  return (
    <footer className="border-t border-zinc-200 bg-zinc-50">
      <div className="max-w-6xl mx-auto px-6 py-10 grid grid-cols-2 md:grid-cols-5 gap-8 text-sm">
        <div className="col-span-2 space-y-3">
          <div className="flex items-center gap-2">
            <img src="/hero-logo.svg" alt="MCP Hero" className="h-6 w-auto" />
            <span className="font-semibold text-zinc-900">MCP Hero</span>
          </div>
          <p className="text-zinc-600 max-w-sm">
            One gateway for every MCP server your team uses — with roles, audit, and OAuth built
            in.
          </p>
        </div>
        <div className="space-y-2">
          <p className="font-medium text-zinc-900">Product</p>
          <ul className="space-y-1.5 text-zinc-600">
            <li>
              <Link to="/pricing" className="hover:text-zinc-900">
                Pricing
              </Link>
            </li>
            <li>
              <Link to="/docs" className="hover:text-zinc-900">
                Docs
              </Link>
            </li>
          </ul>
        </div>
        <div className="space-y-2">
          <p className="font-medium text-zinc-900">Company</p>
          <ul className="space-y-1.5 text-zinc-600">
            <li>
              <Link to="/contact" className="hover:text-zinc-900">
                Contact
              </Link>
            </li>
            <li>
              <Link to="/support" className="hover:text-zinc-900">
                Support
              </Link>
            </li>
          </ul>
        </div>
        <div className="space-y-2">
          <p className="font-medium text-zinc-900">Legal</p>
          <ul className="space-y-1.5 text-zinc-600">
            <li>
              <Link to="/privacy" className="hover:text-zinc-900">
                Privacy
              </Link>
            </li>
            <li>
              <Link to="/security" className="hover:text-zinc-900">
                Security
              </Link>
            </li>
            <li>
              <Link to="/terms" className="hover:text-zinc-900">
                Terms
              </Link>
            </li>
          </ul>
        </div>
      </div>
      <div className="border-t border-zinc-200">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between gap-4 text-xs text-zinc-500">
          <span>Copyright © 2026 Nitsan Seniak. All rights reserved.</span>
          <a
            href="https://github.com/nseniak/mcphero"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="MCP Hero on GitHub"
            className="flex items-center gap-1.5 hover:text-zinc-900"
          >
            <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path>
            </svg>
            GitHub
          </a>
        </div>
      </div>
    </footer>
  );
}

export function MarketingLayout() {
  const { user } = useAuth();
  useAnalyticsPageView("marketing", !!user);
  return (
    <div className="min-h-screen flex flex-col bg-white">
      <TopNav />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  );
}
