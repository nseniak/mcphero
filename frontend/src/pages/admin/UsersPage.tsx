import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import {
  fetchUsers,
  fetchRoles,
  addUser,
  removeUser,
} from "../../api/admin";
import type {
  AdminUserInfo,
  RoleSummary,
} from "../../api/types";
import { useTranslation } from "../../i18n/index";
import { useAuth } from "../../hooks/useAuth";
import { useFeatures } from "../../hooks/useFeatures";
import { ConfirmDialog, useConfirm } from "../../components/ConfirmDialog";
import { Trash2, Plus, ChevronDown, X, Link2, Check, Copy } from "lucide-react";
import { track } from "../../lib/analytics";
import { maybeHandlePlanLimit } from "../../lib/planLimits";
import { Tooltip, TooltipTrigger, TooltipContent } from "../../components/ui/tooltip";
import { RoleBadge } from "../../components/RoleBadge";

function InviteLinkCard() {
  const { user } = useAuth();
  const { mode } = useFeatures();
  const [copied, setCopied] = useState(false);

  if (mode !== "cloud" || !user?.current_org) return null;

  const url = `${window.location.origin}/orgs/${user.current_org.slug}/join`;

  function handleCopy() {
    navigator.clipboard.writeText(url).then(() => {
      track("invite_link_copied", {});
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="mt-4 rounded-lg border border-zinc-200 border-l-4 border-l-blue-500 bg-blue-50/40 p-4">
      <div className="flex items-start gap-3">
        <div className="shrink-0 mt-0.5 text-blue-600">
          <Link2 size={16} />
        </div>
        <div className="flex-1 min-w-0 space-y-2">
          <div>
            <p className="text-sm font-medium text-zinc-900">Invite team members</p>
            <p className="mt-0.5 text-xs text-zinc-600">
              After you add a team member below, send them this link so they can join{" "}
              <span className="font-medium text-zinc-900">{user.current_org.display_name}</span>.
              Only team members can sign in through it.
            </p>
          </div>
          <div className="flex items-stretch gap-2">
            <code className="flex-1 min-w-0 px-2 py-1.5 bg-white border border-zinc-200 rounded text-xs text-zinc-700 truncate">
              {url}
            </code>
            <button
              onClick={handleCopy}
              className={`shrink-0 px-3 py-1.5 text-xs rounded flex items-center gap-1.5 transition-colors ${
                copied
                  ? "bg-green-50 text-green-700 border border-green-200"
                  : "bg-zinc-900 text-white hover:bg-zinc-800"
              }`}
            >
              {copied ? <><Check size={12} /> Copied</> : <><Copy size={12} /> Copy link</>}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function UsersPage() {
  const { t } = useTranslation();
  const { user: currentUser } = useAuth();
  const orgSlug = useOrgSlug();
  const { confirm, dialogProps } = useConfirm();
  const [users, setUsers] = useState<AdminUserInfo[]>([]);
  const [roles, setRoles] = useState<RoleSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [searchParams] = useSearchParams();
  const [roleFilter, setRoleFilter] = useState<string | null>(searchParams.get("role"));
  const [showRoleDropdown, setShowRoleDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showRoleDropdown) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowRoleDropdown(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [showRoleDropdown]);

  // Form state
  const [formEmail, setFormEmail] = useState("");
  const [formRole, setFormRole] = useState("");
  const [formError, setFormError] = useState("");

  const isEmailValid = (email: string): boolean =>
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);

  const reload = () => {
    Promise.all([fetchUsers(), fetchRoles()])
      .then(([u, r]) => {
        setUsers(u);
        setRoles(r);
      })
      // Don't let a failed load (e.g. a 401 after the session died)
      // escape as an unhandled rejection; the global session-expiry
      // handler in apiFetch routes 401s to the sign-in screen.
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(reload, []);

  const assignableRoles = roles;
  const defaultRole = roles.find((r) => r.is_default);

  const handleAdd = async () => {
    setFormError("");
    try {
      await addUser({
        email: formEmail,
        role: formRole || undefined,
      });
      setShowForm(false);
      setFormEmail("");
      setFormRole("");
      reload();
    } catch (e) {
      if (maybeHandlePlanLimit(e, { source: "add_member_button" })) {
        return;
      }
      setFormError(e instanceof Error ? e.message : t("users.failedToAdd"));
    }
  };

  const handleRemove = async (email: string) => {
    const ok = await confirm({
      title: t("common.remove"),
      message: t("users.confirmRemove", { email }),
      confirmLabel: t("common.remove"),
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    await removeUser(email);
    reload();
  };


  const filteredUsers = roleFilter
    ? users.filter((u) => u.role === roleFilter)
    : users;

  const roleCounts = new Map<string, number>();
  for (const r of roles) {
    roleCounts.set(r.name, 0);
  }
  for (const u of users) {
    roleCounts.set(u.role, (roleCounts.get(u.role) ?? 0) + 1);
  }
  const uniqueRoles = [...roleCounts.keys()].sort();

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div>
      {!showForm && (
      <div className="mb-4">
        <h2 className="text-xl font-semibold text-zinc-900">
          {users.length > 0
            ? t("users.titleCount", { count: users.length })
            : t("users.title")}
        </h2>
        <p className="mt-1 text-sm text-zinc-500">{t("users.description")}</p>
        <InviteLinkCard />
      </div>
      )}
      {!showForm && (
      <div className="flex justify-end gap-2 mb-4">
        <button
          onClick={() => setShowForm(true)}
          className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
        >
          <Plus size={14} /> {t("users.addUser")}
        </button>
      </div>
      )}

      {showForm && (
        <div className="border border-zinc-200 rounded-lg p-4 mb-4 bg-zinc-50">
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-zinc-700 mb-1">
                {t("users.formEmail")}
              </label>
              <input
                value={formEmail}
                onChange={(e) => setFormEmail(e.target.value)}
                className={`w-full px-3 py-2 border rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900 focus:border-transparent ${
                  formEmail && !isEmailValid(formEmail)
                    ? "border-red-300"
                    : "border-zinc-300"
                }`}
                placeholder={t("users.placeholderEmail")}
              />
              {(formEmail && !isEmailValid(formEmail)) && (
                <p className="mt-1 text-xs text-red-500">{t("users.invalidEmail")}</p>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-zinc-700 mb-1">
                {t("users.formRole")}
              </label>
              <select
                value={formRole}
                onChange={(e) => setFormRole(e.target.value)}
                className="w-48 px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900 focus:border-transparent"
              >
                <option value="">
                  {defaultRole
                    ? t("users.defaultRole", { role: defaultRole.name })
                    : t("users.selectRole")}
                </option>
                {assignableRoles.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name}
                    {r.is_admin && r.name.toLowerCase() !== t("auth.adminBadge").toLowerCase() ? ` (${t("auth.adminBadge").toLowerCase()})` : ""}
                  </option>
                ))}
              </select>
            </div>
            {formError && (
              <p className="text-sm text-red-600">{formError}</p>
            )}
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowForm(false)}
                className="px-3 py-1.5 border border-zinc-300 text-zinc-700 text-sm rounded hover:bg-zinc-100"
              >
                {t("common.cancel")}
              </button>
              <button
                onClick={handleAdd}
                disabled={!formEmail || !isEmailValid(formEmail)}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {t("common.add")}
              </button>
            </div>
          </div>
        </div>
      )}

      {!showForm && (users.length === 0 ? (
        <p className="text-zinc-400 text-sm">
          {t("users.noUsers")}
        </p>
      ) : (
        <div className="border border-zinc-200 rounded-lg overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm table-fixed">
            <thead className="bg-zinc-50 text-zinc-600">
              <tr>
                <th className="text-left px-4 py-2 font-medium w-[40%]">{t("users.headerUser")}</th>
                <th className="text-left px-4 py-2 font-medium w-[20%]">
                  <div className="relative inline-block" ref={dropdownRef}>
                    <button
                      onClick={() => setShowRoleDropdown(!showRoleDropdown)}
                      className="inline-flex items-center gap-1 hover:text-zinc-900"
                    >
                      {roleFilter
                        ? <>{roleFilter} <span className="text-zinc-400">({roleCounts.get(roleFilter)})</span></>
                        : t("users.headerRole")}
                      {roleFilter ? (
                        <X
                          size={12}
                          className="text-zinc-400 hover:text-zinc-600"
                          onClick={(e) => {
                            e.stopPropagation();
                            setRoleFilter(null);
                            setShowRoleDropdown(false);
                          }}
                        />
                      ) : (
                        <ChevronDown size={12} />
                      )}
                    </button>
                    {showRoleDropdown && (
                      <div className="absolute top-full left-0 mt-1 bg-white border border-zinc-200 rounded shadow-lg z-10 min-w-[120px]">
                        {roleFilter && (
                          <button
                            onClick={() => { setRoleFilter(null); setShowRoleDropdown(false); }}
                            className="block w-full text-left px-3 py-1.5 text-xs text-zinc-500 hover:bg-zinc-50"
                          >
                            All roles
                          </button>
                        )}
                        {uniqueRoles.map((role) => (
                          <button
                            key={role}
                            onClick={() => { setRoleFilter(role); setShowRoleDropdown(false); }}
                            className={`block w-full text-left px-3 py-1.5 text-xs hover:bg-zinc-50 ${
                              roleFilter === role ? "text-blue-600 font-medium" : "text-zinc-700"
                            }`}
                          >
                            {role}
                            <span className="ml-1 text-zinc-400">({roleCounts.get(role)})</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </th>
                <th className="px-4 py-2 text-left font-medium">Status</th>
                <th className="w-10"></th>
              </tr>
            </thead>
            <tbody>
              {filteredUsers.map((user) => (
                <tr key={user.email} className="border-t border-zinc-100 hover:bg-zinc-50">
                  <td className="px-4 py-2">
                    <Link
                      to={`/orgs/${orgSlug}/admin/team/${encodeURIComponent(user.email)}`}
                      className="text-blue-600 hover:underline font-medium"
                    >
                      {user.email}
                    </Link>
                    {user.is_admin && user.role.toLowerCase() !== t("auth.adminBadge").toLowerCase() && (
                      <span className="ml-1.5 px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded text-xs">
                        {t("auth.adminBadge").toLowerCase()}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    <RoleBadge role={user.role} isAdmin={user.is_admin} />
                  </td>
                  <td className="px-4 py-2">
                    {user.status === "active" ? (
                      <span className="px-1.5 py-0.5 bg-green-100 text-green-700 rounded text-xs font-medium">
                        Joined
                      </span>
                    ) : (
                      <span className="px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded text-xs font-medium">
                        Pending
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-right">
                    {user.email !== currentUser?.email && (
                      <Tooltip>
                        <TooltipTrigger
                          onClick={() => handleRemove(user.email)}
                          className="p-1 text-zinc-300 hover:text-red-500 rounded"
                        >
                          <Trash2 size={14} />
                        </TooltipTrigger>
                        <TooltipContent>{t("common.remove")}</TooltipContent>
                      </Tooltip>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      <ConfirmDialog {...dialogProps} />
    </div>
  );
}
