import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { AuthContext, useAuth, useAuthProvider } from "./hooks/useAuth";
import { useFeatures } from "./hooks/useFeatures";
import { fetchGatewayConfig } from "./api/user";
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

function DefaultRedirect() {
  const { user, loading } = useAuth();
  const { mode, isLoading: featuresLoading } = useFeatures();
  const { data: gateway, isLoading: gatewayLoading } = useQuery({
    queryKey: ["gateway-config"],
    queryFn: fetchGatewayConfig,
    enabled: !!user && !user.is_admin,
  });

  if (loading || featuresLoading || !user) {
    return null;
  }
  // Cloud mode: user signed in but has no org → must create one.
  if (mode === "cloud" && !user.current_org) {
    return <Navigate to="/signup" replace />;
  }

  const slug = user.current_org?.slug ?? "default";

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
