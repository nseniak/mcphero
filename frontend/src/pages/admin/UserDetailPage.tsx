import { useEffect, useState } from "react";
import { useParams, Link, useNavigate } from "react-router";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import {
  fetchUsers,
  fetchRoles,
  removeUser,
  setUserRole,
} from "../../api/admin";
import type {
  AdminUserInfo,
  RoleSummary,
} from "../../api/types";
import { useTranslation } from "../../i18n/index";
import { useAuth } from "../../hooks/useAuth";
import { ConfirmDialog, useConfirm } from "../../components/ConfirmDialog";
import { ArrowLeft, Pencil, Trash2 } from "lucide-react";
import { RoleBadge } from "../../components/RoleBadge";
import { SettingsCard, SettingsField } from "../../components/SettingsCard";

export function UserDetailPage() {
  const { t } = useTranslation();
  const { user: currentUser } = useAuth();
  const { email } = useParams<{ email: string }>();
  const navigate = useNavigate();
  const orgSlug = useOrgSlug();
  const { confirm, dialogProps } = useConfirm();
  const [user, setUser] = useState<AdminUserInfo | null>(null);
  const [roles, setRoles] = useState<RoleSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editRole, setEditRole] = useState("");

  const decodedEmail = email ? decodeURIComponent(email) : "";
  const isSelf = decodedEmail === currentUser?.email;

  const reload = () => {
    if (!decodedEmail) return;
    Promise.all([fetchUsers(), fetchRoles()])
      .then(([users, r]) => {
        const found = users.find((u) => u.email === decodedEmail) ?? null;
        setUser(found);
        setRoles(r);
        if (found) setEditRole(found.role);
      })
      // Don't let a failed load (e.g. a 401 after the session died)
      // escape as an unhandled rejection; the global session-expiry
      // handler in apiFetch routes 401s to the sign-in screen.
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(reload, [decodedEmail]);

  const assignableRoles = roles;

  const enterEdit = () => {
    if (user) setEditRole(user.role);
    setEditing(true);
  };

  const cancelEdit = () => {
    if (user) setEditRole(user.role);
    setEditing(false);
  };

  const handleSave = async () => {
    if (!decodedEmail || !user) return;
    if (editRole !== user.role) {
      const updated = await setUserRole(decodedEmail, editRole);
      setUser(updated);
    }
    setEditing(false);
    reload();
  };

  const handleRemove = async () => {
    if (!decodedEmail) return;
    const ok = await confirm({
      title: t("common.remove"),
      message: t("users.confirmRemove", { email: decodedEmail }),
      confirmLabel: t("common.remove"),
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    await removeUser(decodedEmail);
    navigate(`/orgs/${orgSlug}/admin/team`);
  };

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;
  if (!user) return <p className="text-red-500">{t("userDetail.notFound")}</p>;

  const anyDirty = editRole !== user.role;

  return (
    <div>
      <Link
        to={`/orgs/${orgSlug}/admin/team`}
        className="inline-flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-700 mb-4"
      >
        <ArrowLeft size={14} /> {t("userDetail.backToUsers")}
      </Link>

      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-semibold text-zinc-900">
            {user.email}
          </h2>
          {user.is_admin && user.role.toLowerCase() !== t("auth.adminBadge").toLowerCase() && (
            <span className="px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded text-xs">
              {t("auth.adminBadge").toLowerCase()}
            </span>
          )}
        </div>
      </div>

      {/* Settings section */}
      <SettingsCard
        title="Settings"
        actions={
          editing ? (
            <div className="flex items-center gap-2">
              <button
                onClick={handleSave}
                disabled={!anyDirty}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Save
              </button>
              <button
                onClick={cancelEdit}
                className="px-3 py-1.5 bg-zinc-100 text-zinc-700 text-sm rounded hover:bg-zinc-200"
              >
                {t("common.cancel")}
              </button>
            </div>
          ) : !isSelf ? (
            <button
              onClick={enterEdit}
              className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
            >
              <Pencil size={14} /> Edit
            </button>
          ) : null
        }
      >
        <SettingsField label={t("userDetail.labelRole")}>
          {editing ? (
            <select
              value={editRole}
              onChange={(e) => setEditRole(e.target.value)}
              className="px-2.5 py-1.5 border border-zinc-200 rounded text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
            >
              {assignableRoles.map((r) => (
                <option key={r.name} value={r.name}>
                  {r.name}
                  {r.is_admin && r.name.toLowerCase() !== t("auth.adminBadge").toLowerCase() ? ` (${t("auth.adminBadge").toLowerCase()})` : ""}
                </option>
              ))}
            </select>
          ) : (
            <RoleBadge role={user.role} isAdmin={user.is_admin} />
          )}
        </SettingsField>
      </SettingsCard>

      {editing && !isSelf && (
        <div className="mb-6 flex justify-end">
          <button
            onClick={handleRemove}
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-red-500 border border-red-200 rounded hover:bg-red-50 hover:border-red-300"
          >
            <Trash2 size={12} />
            {t("common.remove")}
          </button>
        </div>
      )}

      {/* Permissions link */}
      <div className="text-sm">
        <span className="text-zinc-500">Permissions are managed at the role level. </span>
        <Link
          to={`/orgs/${orgSlug}/admin/permissions?role=${encodeURIComponent(user.role)}`}
          className="text-blue-500 hover:underline"
        >
          View {user.role} permissions
        </Link>
      </div>

      <ConfirmDialog {...dialogProps} />
    </div>
  );
}
