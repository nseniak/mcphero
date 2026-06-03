import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, Trash2, Building2 } from "lucide-react";
import { checkSlug, fetchOrgs, createOrg, deleteOrg, switchOrg, fetchOrgInfo } from "../api/orgs";
import type { OrgListEntry } from "../api/types";
import { useConfirm, ConfirmDialog } from "../components/ConfirmDialog";
import { PlanBadge } from "../components/layout/PlanBadge";
import { RoleBadge } from "../components/RoleBadge";
import { UpstreamCapacityPills } from "../components/UpstreamCapacityPills";
import { FieldHint } from "../components/ui/field-hint";

const SLUG_REGEX = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/;
const SLUG_CHARS = /[^a-z0-9-]/g;

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
}

function validateSlug(value: string): string | null {
  if (!value) return null;
  if (value.startsWith("-") || value.endsWith("-")) return "Cannot start or end with a hyphen";
  if (value.length < 3) return "Must be at least 3 characters";
  if (value.length > 40) return "Must be at most 40 characters";
  if (!SLUG_REGEX.test(value)) return "Invalid format";
  return null;
}

export function OrganizationsPage() {
  const { confirm, dialogProps } = useConfirm();
  const [orgs, setOrgs] = useState<OrgListEntry[]>([]);
  const [currentSlug, setCurrentSlug] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [slugHint, setSlugHint] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(null);

  const checkSlugAvailability = useCallback((value: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const localError = validateSlug(value);
    setSlugHint(localError);
    if (localError || !value) return;
    debounceRef.current = setTimeout(async () => {
      try {
        const result = await checkSlug(value);
        if (!result.available) {
          setSlugHint(result.reason);
        }
      } catch {
        // Ignore network errors during validation
      }
    }, 400);
  }, []);

  const reload = () => {
    fetchOrgs().then((data) => {
      setOrgs(data.orgs);
      setCurrentSlug(data.current_slug);
    });
  };

  useEffect(() => {
    fetchOrgs()
      .then((data) => {
        setOrgs(data.orgs);
        setCurrentSlug(data.current_slug);
      })
      .finally(() => setLoading(false));
  }, []);

  function handleNameChange(value: string) {
    setDisplayName(value);
    if (!slugEdited) {
      const suggested = slugify(value);
      setSlug(suggested);
      checkSlugAvailability(suggested);
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!displayName.trim() || !slug.trim()) return;
    setSlugTouched(true);
    const localError = validateSlug(slug);
    if (localError) {
      setSlugHint(localError);
      return;
    }
    if (slugHint) return;
    setSubmitting(true);
    setError(null);
    try {
      await createOrg(slug.trim(), displayName.trim());
      setDisplayName("");
      setSlug("");
      setSlugEdited(false);
      setSlugTouched(false);
      setShowForm(false);
      reload();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to create organization";
      if (msg.toLowerCase().includes("short name") || msg.toLowerCase().includes("taken") || msg.toLowerCase().includes("reserved")) {
        setSlugTouched(true);
        setSlugHint(msg);
      } else {
        setError(msg);
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(org: OrgListEntry) {
    let info;
    try {
      info = await fetchOrgInfo(org.slug);
    } catch {
      info = { member_count: 0 };
    }
    const ok = await confirm({
      title: `Delete ${org.display_name}`,
      message:
        `This will permanently delete "${org.display_name}" and all its data. ` +
        `${info.member_count} member${info.member_count === 1 ? "" : "s"} will lose access. ` +
        `This cannot be undone.`,
      confirmLabel: "Delete organization",
      cancelLabel: "Cancel",
      destructive: true,
      requireText: "delete",
    });
    if (!ok) return;
    try {
      const wasCurrent = org.slug === currentSlug;
      await deleteOrg(org.slug);
      if (wasCurrent) {
        // Deleted the current org — switch to another or go to signup
        const updated = await fetchOrgs();
        if (updated.orgs.length > 0) {
          await switchOrg(updated.orgs[0].slug);
          // Hand off to DefaultRedirect so the role in the new
          // current org picks the right landing.
          window.location.href = "/app";
        } else {
          window.location.href = "/signup";
        }
        return;
      }
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete organization");
    }
  }

  async function handleSwitch(slug: string) {
    try {
      await switchOrg(slug);
      // Hand off to DefaultRedirect so the role in the target org
      // picks the right landing instead of forcing /admin/upstream.
      window.location.href = "/app";
    } catch {
      // Swallow
    }
  }

  if (loading) {
    return (
      <div className="p-6 text-zinc-500 text-sm">Loading organizations...</div>
    );
  }

  return (
    <div className="p-6 max-w-4xl">
      <ConfirmDialog {...dialogProps} />

      <div className="mb-4">
        <h2 className="text-xl font-semibold text-zinc-900">
          Organizations{" "}
          <span className="text-zinc-400 font-normal text-base">
            ({orgs.length})
          </span>
        </h2>
        <p className="mt-1 text-sm text-zinc-500">
          Manage your organizations. Each organization has its own MCPs, team,
          and permissions.
        </p>
      </div>

      <div className="flex justify-end gap-2 mb-4">
        <button
          onClick={() => setShowForm(!showForm)}
          className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
        >
          {showForm ? (
            "Cancel"
          ) : (
            <>
              <Plus size={14} /> New organization
            </>
          )}
        </button>
      </div>

      {showForm && (
        <form
          onSubmit={handleCreate}
          className="border border-zinc-200 rounded-lg p-4 mb-4 bg-zinc-50"
        >
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-zinc-700 mb-1">
                Organization name
              </label>
              <input
                value={displayName}
                onChange={(e) => handleNameChange(e.target.value)}
                placeholder="Acme Corp"
                className="w-full px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900"
                autoFocus
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-zinc-700 mb-1">
                Short name
              </label>
              <input
                value={slug}
                onFocus={() => setSlugTouched(true)}
                onChange={(e) => {
                  const raw = e.target.value.toLowerCase().replace(SLUG_CHARS, "");
                  setSlug(raw);
                  setSlugEdited(true);
                  checkSlugAvailability(raw);
                }}
                placeholder="acme-corp"
                className="w-full px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900"
              />
              {slugTouched && slugHint ? (
                <FieldHint>{slugHint}</FieldHint>
              ) : (
                <p className="mt-1 text-xs text-zinc-400">
                  Used in URLs. Lowercase letters, numbers, and hyphens.
                </p>
              )}
            </div>
            {error && (
              <p className="text-sm text-red-600">{error}</p>
            )}
            <button
              type="submit"
              disabled={submitting || !displayName.trim() || !slug.trim() || (slugTouched && !!slugHint)}
              className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      )}

      {orgs.length === 0 && !showForm ? (
        <div className="flex flex-col items-center justify-center py-20 space-y-6">
          <button
            onClick={() => setShowForm(true)}
            className="w-80 p-6 border-2 border-dashed border-zinc-300 rounded-xl hover:border-blue-400 hover:bg-blue-50 transition-colors"
          >
            <div className="flex items-center justify-center w-12 h-12 mx-auto mb-3 bg-blue-100 rounded-full">
              <Plus size={24} className="text-blue-600" />
            </div>
            <p className="text-base font-semibold text-zinc-900">
              Create organization
            </p>
            <p className="text-sm text-zinc-500 mt-1">
              Get started by creating your first organization
            </p>
          </button>
        </div>
      ) : (
        <div className="border border-zinc-200 rounded-lg overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm table-fixed">
            <thead className="bg-zinc-50 text-zinc-600">
              <tr>
                <th className="text-left px-4 py-2 font-medium w-[24%]">
                  Organization
                </th>
                <th className="text-left px-4 py-2 font-medium w-[10%]">
                </th>
                <th className="text-left px-4 py-2 font-medium w-[12%]">
                  Your role
                </th>
                <th className="text-right px-4 py-2 font-medium w-[8%]">
                  Team
                </th>
                <th className="text-left px-4 py-2 font-medium w-[17%]">
                  Upstreams
                </th>
                <th className="text-left px-4 py-2 font-medium w-[19%]">
                  Plan
                </th>
                <th className="w-10"></th>
              </tr>
            </thead>
            <tbody>
              {orgs.map((org) => {
                const isCurrent = org.slug === currentSlug;
                const isAdmin = org.is_admin;
                return (
                  <tr
                    key={org.slug}
                    className="border-t border-zinc-100 hover:bg-zinc-50"
                  >
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-2">
                        <Building2 size={16} className="text-zinc-400 shrink-0" />
                        <div className="flex flex-col min-w-0">
                          <span className="font-medium text-zinc-900 truncate">
                            {org.display_name}
                          </span>
                          <span className="text-zinc-400 text-xs truncate">
                            {org.slug}
                          </span>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-2">
                      {isCurrent ? (
                        <span className="px-1.5 py-0.5 bg-green-100 text-green-700 rounded text-xs font-medium">
                          Current
                        </span>
                      ) : (
                        <button
                          onClick={() => handleSwitch(org.slug)}
                          className="text-blue-600 hover:underline text-xs"
                        >
                          Switch
                        </button>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <RoleBadge role={org.role} />
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-zinc-700">
                      {org.member_count}
                    </td>
                    <td className="px-4 py-2">
                      <UpstreamCapacityPills
                        plan={org.plan}
                        httpCount={org.http_upstream_count}
                        stdioCount={org.stdio_upstream_count}
                      />
                    </td>
                    <td className="px-4 py-2">
                      <PlanBadge
                        plan={org.plan}
                        isAdmin={org.is_admin}
                        source="manage_page_row"
                      />
                    </td>
                    <td className="px-4 py-2 text-right">
                      {isAdmin && (
                        <button
                          onClick={() => handleDelete(org)}
                          className="p-1 text-zinc-300 hover:text-red-500 rounded"
                          title="Delete organization"
                        >
                          <Trash2 size={14} />
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
