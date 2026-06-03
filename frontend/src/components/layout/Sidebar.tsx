import { Link, NavLink } from "react-router";
import { useAuth } from "../../hooks/useAuth";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import { useUpstreams } from "../../hooks/useUpstreams";
import { useTranslation, type TranslationKey } from "../../i18n/index";
import { ScrollText, Users, Plug, Shield, Radio, Terminal, Sparkles, LayoutDashboard, Activity, Building2, UserCog, Server, FileText, KeyRound, Cog } from "lucide-react";

function McpIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 180 180" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M18 84.8528L85.8822 16.9706C95.2548 7.59798 110.451 7.59798 119.823 16.9706V16.9706C129.196 26.3431 129.196 41.5391 119.823 50.9117L68.5581 102.177" stroke="currentColor" strokeWidth="12" strokeLinecap="round"/>
      <path d="M69.2652 101.47L119.823 50.9117C129.196 41.5391 144.392 41.5391 153.765 50.9117L154.118 51.2652C163.491 60.6378 163.491 75.8338 154.118 85.2063L92.7248 146.6C89.6006 149.724 89.6006 154.789 92.7248 157.913L105.331 170.52" stroke="currentColor" strokeWidth="12" strokeLinecap="round"/>
      <path d="M102.853 33.9411L52.6482 84.1457C43.2756 93.5183 43.2756 108.714 52.6482 118.087V118.087C62.0208 127.459 77.2167 127.459 86.5893 118.087L136.794 67.8822" stroke="currentColor" strokeWidth="12" strokeLinecap="round"/>
    </svg>
  );
}

type IconComponent = React.ComponentType<{ size?: number }>;

/** Paths relative to the org prefix. The prefix is prepended at render time. */
const adminLinks: { path: string; labelKey: TranslationKey; icon: IconComponent }[] = [
  { path: "/admin/upstream", labelKey: "sidebar.teamMcps", icon: McpIcon },
  { path: "/admin/gateway", labelKey: "sidebar.gateway", icon: Radio },
  { path: "/admin/team", labelKey: "sidebar.team", icon: Users },
  { path: "/admin/permissions", labelKey: "sidebar.access", icon: Shield },
  { path: "/admin/audit", labelKey: "sidebar.auditLog", icon: ScrollText },
  { path: "/admin/admin-mcp", labelKey: "sidebar.adminMcp", icon: Terminal },
];

// Operator-only sidebar links (egress denylist) were removed along
// with the runner-side Envoy enforcer; the section renders empty
// for superadmins until a new operator surface is reintroduced.
const operatorLinks: { path: string; labelKey: TranslationKey; icon: IconComponent }[] = [];

/**
 * Cross-org superadmin links. Paths are absolute (no org-slug prefix)
 * because the superadmin dashboard is unscoped — it browses every
 * tenant. Rendered only when ``user.is_superadmin`` is true.
 */
const superadminLinks: { path: string; labelKey: TranslationKey; icon: IconComponent }[] = [
  { path: "/superadmin", labelKey: "sidebar.superadminOverview", icon: LayoutDashboard },
  { path: "/superadmin/orgs", labelKey: "sidebar.superadminOrgs", icon: Building2 },
  { path: "/superadmin/users", labelKey: "sidebar.superadminUsers", icon: UserCog },
  { path: "/superadmin/upstreams", labelKey: "sidebar.superadminUpstreams", icon: Server },
  { path: "/superadmin/audit", labelKey: "sidebar.superadminAudit", icon: FileText },
  { path: "/superadmin/auth-health", labelKey: "sidebar.superadminAuthHealth", icon: KeyRound },
  { path: "/superadmin/system", labelKey: "sidebar.superadminSystem", icon: Cog },
  { path: "/superadmin/test-observability", labelKey: "sidebar.superadminTestObservability", icon: Activity },
];

const userLinks: { path: string; labelKey: TranslationKey; icon: IconComponent }[] = [
  { path: "/connect", labelKey: "sidebar.connect", icon: Plug },
  { path: "/my-tools", labelKey: "sidebar.myMcps", icon: Sparkles },
];

type SidebarProps = {
  isOpen: boolean;
  onClose: () => void;
};

export function Sidebar({ isOpen, onClose }: SidebarProps) {
  const { user } = useAuth();
  const slug = useOrgSlug();
  // Counts feed the admin section's MCP badge only; skip the
  // /api/admin/upstreams fetch for non-admins (it 403s for them).
  const { connectedCount, disconnectedCount, errorCount } = useUpstreams({
    enabled: !!user?.is_admin,
  });
  const { t } = useTranslation();

  const prefix = `/orgs/${slug}`;

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/40 z-30 md:hidden"
          onClick={onClose}
          aria-hidden
        />
      )}
      <aside
        className={`fixed md:static inset-y-0 left-0 z-40 w-60 border-r border-zinc-200 bg-zinc-50 flex flex-col h-full shrink-0 transform transition-transform duration-200 ease-in-out md:translate-x-0 ${
          isOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        }`}
      >
      <Link
        to="/home"
        onClick={onClose}
        className="p-4 border-b border-zinc-200 flex items-center gap-2 hover:bg-zinc-100 transition-colors"
      >
        <img src="/hero-logo.svg" alt="MCP Hero" className="h-10 w-auto" />
        <h1 className="text-lg font-semibold text-zinc-900">{t("sidebar.appName")}</h1>
      </Link>
      <nav className="flex-1 p-2 space-y-1">
        {user?.is_admin && (
          <>
            <div className="px-2 py-1 text-xs font-medium text-zinc-500 uppercase tracking-wider">
              {t("sidebar.adminSection")}
            </div>
            {adminLinks.map(({ path, labelKey, icon: Icon }) => {
              const to = `${prefix}${path}`;
              const isMcps = path === "/admin/upstream";
              return (
                <NavLink
                  key={path}
                  to={to}
                  onClick={onClose}
                  className={({ isActive }) =>
                    `flex items-center gap-2 px-2 py-1.5 rounded text-sm ${
                      isActive
                        ? "bg-zinc-200 text-zinc-900 font-medium"
                        : "text-zinc-600 hover:bg-zinc-100"
                    }`
                  }
                >
                  <Icon size={16} />
                  {t(labelKey)}
                  {isMcps && (connectedCount > 0 || disconnectedCount > 0 || errorCount > 0) && (
                    <span className="ml-auto flex items-center gap-1 text-[10px]">
                      {connectedCount > 0 && (
                        <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-green-100 text-green-700 font-medium">
                          {connectedCount}
                        </span>
                      )}
                      {disconnectedCount > 0 && (
                        <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-zinc-300 text-zinc-600 font-medium">
                          {disconnectedCount}
                        </span>
                      )}
                      {errorCount > 0 && (
                        <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-red-500 text-white font-medium">
                          {errorCount}
                        </span>
                      )}
                    </span>
                  )}
                </NavLink>
              );
            })}
          </>
        )}
        {user?.is_superadmin && (
          <>
            {operatorLinks.length > 0 && (
              <>
                <div className="px-2 py-1 text-xs font-medium text-zinc-500 uppercase tracking-wider mt-4">
                  {t("sidebar.operatorSection")}
                </div>
                {operatorLinks.map(({ path, labelKey, icon: Icon }) => {
                  const to = `${prefix}${path}`;
                  return (
                    <NavLink
                      key={path}
                      to={to}
                      className={({ isActive }) =>
                        `flex items-center gap-2 px-2 py-1.5 rounded text-sm ${
                          isActive
                            ? "bg-zinc-200 text-zinc-900 font-medium"
                            : "text-zinc-600 hover:bg-zinc-100"
                        }`
                      }
                    >
                      <Icon size={16} />
                      {t(labelKey)}
                    </NavLink>
                  );
                })}
              </>
            )}
            <div className="px-2 py-1 text-xs font-medium text-zinc-500 uppercase tracking-wider mt-4">
              {t("sidebar.superadminSection")}
            </div>
            {superadminLinks.map(({ path, labelKey, icon: Icon }) => (
              <NavLink
                key={path}
                to={path}
                end={path === "/superadmin"}
                onClick={onClose}
                className={({ isActive }) =>
                  `flex items-center gap-2 px-2 py-1.5 rounded text-sm ${
                    isActive
                      ? "bg-zinc-200 text-zinc-900 font-medium"
                      : "text-zinc-600 hover:bg-zinc-100"
                  }`
                }
              >
                <Icon size={16} />
                {t(labelKey)}
              </NavLink>
            ))}
          </>
        )}
        <div className="px-2 py-1 text-xs font-medium text-zinc-500 uppercase tracking-wider mt-4">
          {t("sidebar.userSection")}
        </div>
        {userLinks.map(({ path, labelKey, icon: Icon }) => {
          const to = `${prefix}${path}`;
          return (
            <NavLink
              key={path}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-2 px-2 py-1.5 rounded text-sm ${
                  isActive
                    ? "bg-zinc-200 text-zinc-900 font-medium"
                    : "text-zinc-600 hover:bg-zinc-100"
                }`
              }
            >
              <Icon size={16} />
              {t(labelKey)}
            </NavLink>
          );
        })}
      </nav>
      {user && (
        <div className="p-3 border-t border-zinc-200 text-xs text-zinc-500 truncate">
          {user.email}
        </div>
      )}
      </aside>
    </>
  );
}
