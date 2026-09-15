import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { AuthContext, useAuth, useAuthProvider } from "./hooks/useAuth";
import { useFeatures } from "./hooks/useFeatures";
import { fetchGatewayConfig } from "./api/user";
import { getLoginUrl } from "./api/auth";
import { TooltipProvider } from "./components/ui/tooltip";
import { setUpgradeDialogOpener } from "./lib/planLimits";
import type { PlanLimitDialogState } from "./lib/planLimits";
import { UpgradeToTeamDialog } from "./components/UpgradeToTeamDialog";
import { DashboardLayout } from "./components/layout/DashboardLayout";
import { MarketingLayout } from "./components/layout/MarketingLayout";
import { UpstreamsPage } from "./pages/admin/UpstreamsPage";
import { UpstreamDetailPage } from "./pages/admin/UpstreamDetailPage";
import { AuditPage } from "./pages/admin/AuditPage";
import { UsersPage } from "./pages/admin/UsersPage";
import { UserDetailPage } from "./pages/admin/UserDetailPage";
import { AccessPage } from "./pages/admin/AccessPage";
import { GatewayPage } from "./pages/admin/GatewayPage";
import { ServiceTokensPage } from "./pages/admin/ServiceTokensPage";
import { AdminMcpPage } from "./pages/admin/AdminMcpPage";
import { ConnectPage } from "./pages/user/ConnectPage";
import { UserMcpsPage } from "./pages/user/UserMcpsPage";
import { SignupPage } from "./pages/SignupPage";
import { JoinPage } from "./pages/JoinPage";
import { OrganizationsPage } from "./pages/OrganizationsPage";
import { HomePage } from "./pages/marketing/HomePage";
import { PricingPage } from "./pages/marketing/PricingPage";
import { PrivacyPage } from "./pages/marketing/PrivacyPage";
import { SecurityPage } from "./pages/marketing/SecurityPage";
import { SupportPage } from "./pages/marketing/SupportPage";
import { TermsPage } from "./pages/marketing/TermsPage";
import { DocsPage } from "./pages/marketing/DocsPage";
import { ContactPage } from "./pages/marketing/ContactPage";
import { SuperadminGuard } from "./pages/superadmin/SuperadminGuard";
import { OverviewPage as SuperadminOverviewPage } from "./pages/superadmin/OverviewPage";
import { OrgsListPage as SuperadminOrgsListPage } from "./pages/superadmin/OrgsListPage";
import { UsersListPage as SuperadminUsersListPage } from "./pages/superadmin/UsersListPage";
import { UserDetailPage as SuperadminUserDetailPage } from "./pages/superadmin/UserDetailPage";
import { UpstreamsListPage as SuperadminUpstreamsListPage } from "./pages/superadmin/UpstreamsListPage";
import { AuditPage as SuperadminAuditPage } from "./pages/superadmin/AuditPage";
import { AuthHealthPage as SuperadminAuthHealthPage } from "./pages/superadmin/AuthHealthPage";
import { SystemPage as SuperadminSystemPage } from "./pages/superadmin/SystemPage";
import { TestObservabilityPage } from "./pages/superadmin/TestObservabilityPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

/** Shared gate for the chrome-less transit routes (``/app``,
 *  ``/my-tools``). Resolves auth + features, hands a logged-out
 *  visitor to the server login route, and yields the org slug the
 *  destination path needs.
 *
 *  Returns ``"pending"`` while still resolving (render nothing),
 *  ``"signup"`` when a signed-in cloud user has no org yet, or the
 *  slug to build a destination from. Keeping this in one place means
 *  a new transit route can't drift from ``/app``'s behaviour. */
type TransitTarget =
  | { status: "pending" }
  | { status: "signup" }
  | { status: "ready"; slug: string };

function useTransitTarget(): TransitTarget {
  const { user, loading } = useAuth();
  const { mode, isLoading: featuresLoading } = useFeatures();

  // Once auth + features have resolved and there's still no user, the
  // visitor is logged out. Hand off to the server login route — the
  // dev_stub email picker in standalone, Google in cloud — instead of
  // rendering nothing. Without this, /app is a permanent blank page for
  // a logged-out user, which is exactly where standalone's "open
  // localhost:8080" lands (/ → /app), so the README's promised email
  // picker never appears.
  const loggedOut = !loading && !featuresLoading && !user;
  useEffect(() => {
    if (loggedOut) {
      window.location.href = getLoginUrl();
    }
  }, [loggedOut]);

  if (loading || featuresLoading || !user) {
    return { status: "pending" };
  }
  // Cloud mode: user signed in but has no org → must create one.
  if (mode === "cloud" && !user.current_org) {
    return { status: "signup" };
  }
  return { status: "ready", slug: user.current_org?.slug ?? "default" };
}

/** Bare ``/my-tools``: resolves to the signed-in person's own org.
 *
 *  The gateway emits this path when a per-user OAuth connection needs
 *  re-authenticating (``tool_router``), and so does the upstream
 *  health-check email — neither knows the viewer's org slug, only the
 *  org id. Without this route the path fell through to ``path="*"``
 *  and the user landed on the marketing homepage.
 *
 *  The query string is preserved: the health-check link carries a
 *  ``?reauth=`` token that its (still to be built) consumer needs. */
function MyToolsRedirect() {
  const target = useTransitTarget();
  const { search } = useLocation();

  if (target.status === "pending") {
    return null;
  }
  if (target.status === "signup") {
    return <Navigate to="/signup" replace />;
  }
  return (
    <Navigate to={`/orgs/${target.slug}/my-tools${search}`} replace />
  );
}

function DefaultRedirect() {
  const { user } = useAuth();
  const target = useTransitTarget();
  const { data: gateway, isLoading: gatewayLoading } = useQuery({
    queryKey: ["gateway-config"],
    queryFn: fetchGatewayConfig,
    enabled: !!user && !user.is_admin,
  });

  if (target.status === "pending" || !user) {
    return null;
  }
  if (target.status === "signup") {
    return <Navigate to="/signup" replace />;
  }

  const slug = target.slug;

  if (user.is_admin) {
    return <Navigate to={`/orgs/${slug}/admin/upstream`} replace />;
  }
  if (gatewayLoading || !gateway) {
    return null;
  }
  const isConnected = gateway.connected_users.includes(user.email);
  return (
    <Navigate
      to={isConnected ? `/orgs/${slug}/my-tools` : `/orgs/${slug}/connect`}
      replace
    />
  );
}

/** Mounts the shared upgrade dialog and wires the
 *  ``handlePlanLimitError`` helper to it. Lives at the app shell so
 *  every mutation handler in the SPA can pop the dialog without
 *  threading callbacks through component trees. */
function UpgradeDialogHost() {
  const [state, setState] = useState<PlanLimitDialogState | null>(null);
  useEffect(() => {
    setUpgradeDialogOpener((next) => setState(next));
    return () => setUpgradeDialogOpener(null);
  }, []);
  if (!state) return null;
  return (
    <UpgradeToTeamDialog
      gate={state.gate}
      current={state.current}
      limit={state.limit}
      source={state.source}
      message={state.message}
      onClose={() => setState(null)}
    />
  );
}

function App() {
  const auth = useAuthProvider();

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
      <AuthContext.Provider value={auth}>
        <UpgradeDialogHost />
        <BrowserRouter>
          <Routes>
            <Route element={<MarketingLayout />}>
              <Route index element={<HomePage />} />
              <Route path="/home" element={<HomePage forceShow />} />
              <Route path="/pricing" element={<PricingPage />} />
              <Route path="/privacy" element={<PrivacyPage />} />
              <Route path="/security" element={<SecurityPage />} />
              <Route path="/support" element={<SupportPage />} />
              <Route path="/terms" element={<TermsPage />} />
              <Route path="/docs" element={<DocsPage />} />
              <Route path="/docs/:slug" element={<DocsPage />} />
              <Route path="/contact" element={<ContactPage />} />
              <Route path="/signup" element={<SignupPage />} />
            </Route>
            <Route path="/orgs/:slug/join" element={<JoinPage />} />
            {/* /app is a chrome-less transit route: DefaultRedirect
                returns null while resolving auth/features, then
                Navigates to the right destination. Putting it inside
                DashboardLayout would paint the dashboard chrome
                during that resolve, giving every "Go to app" click a
                visible flash of sidebar before the real page lands. */}
            <Route path="/app" element={<DefaultRedirect />} />
            {/* Bare /my-tools: same chrome-less transit treatment as
                /app. The gateway and the health-check email both emit
                this path without a slug, so it must resolve to the
                viewer's own org instead of falling to path="*". */}
            <Route path="/my-tools" element={<MyToolsRedirect />} />
            <Route element={<DashboardLayout />}>
              <Route path="/orgs/manage" element={<OrganizationsPage />} />
              <Route path="/orgs/:slug">
                <Route path="admin/upstream" element={<UpstreamsPage />} />
                <Route path="admin/upstream/:id" element={<UpstreamDetailPage />} />
                <Route path="admin/audit" element={<AuditPage />} />
                <Route path="admin/admin-mcp" element={<AdminMcpPage />} />
                <Route path="admin/team" element={<UsersPage />} />
                <Route path="admin/team/:email" element={<UserDetailPage />} />
                <Route path="admin/permissions" element={<AccessPage />} />
                <Route path="admin/gateway" element={<GatewayPage />} />
                <Route path="admin/service-tokens" element={<ServiceTokensPage />} />
                <Route path="connect" element={<ConnectPage />} />
                <Route path="my-tools" element={<UserMcpsPage />} />
              </Route>
            </Route>
            {/* Superadmin dashboard. Cross-org browse + soft actions,
                gated by MCPOLIS_SUPERADMIN_EMAILS at both the route
                level (SuperadminGuard) and the API level. */}
            <Route element={<SuperadminGuard />}>
              <Route element={<DashboardLayout />}>
                <Route path="/superadmin" element={<SuperadminOverviewPage />} />
                <Route
                  path="/superadmin/orgs"
                  element={<SuperadminOrgsListPage />}
                />
                <Route
                  path="/superadmin/users"
                  element={<SuperadminUsersListPage />}
                />
                <Route
                  path="/superadmin/users/:email"
                  element={<SuperadminUserDetailPage />}
                />
                <Route
                  path="/superadmin/upstreams"
                  element={<SuperadminUpstreamsListPage />}
                />
                <Route
                  path="/superadmin/audit"
                  element={<SuperadminAuditPage />}
                />
                <Route
                  path="/superadmin/auth-health"
                  element={<SuperadminAuthHealthPage />}
                />
                <Route
                  path="/superadmin/system"
                  element={<SuperadminSystemPage />}
                />
                <Route
                  path="/superadmin/test-observability"
                  element={<TestObservabilityPage />}
                />
              </Route>
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
