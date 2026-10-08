import { useState } from "react";
import { acceptInvitation, declineInvitation, switchOrg } from "../api/orgs";
import type { InvitationInfo } from "../api/types";
import { useTranslation } from "../i18n/index";

type Busy = { slug: string; action: "join" | "decline" } | null;

/** Join an organization from its invitation, then open it. Shared by the
 *  invitation list below and the Join page. */
export async function joinAndOpen(slug: string): Promise<void> {
  await acceptInvitation(slug);
  await switchOrg(slug);
  // Full reload so the auth context and every cached query pick up the
  // new membership.
  window.location.href = "/app";
}

/** The signed-in person's invitations, each with Join and Decline. An
 *  invitation gives nothing until it is accepted: Join makes them a
 *  member and opens the organization, Decline deletes the invitation.
 *  Renders nothing when there is no invitation. */
export function PendingInvitations({
  invitations,
}: {
  invitations: InvitationInfo[];
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);

  if (invitations.length === 0) return null;

  async function run(slug: string, action: "join" | "decline") {
    setBusy({ slug, action });
    setError(null);
    try {
      if (action === "join") {
        await joinAndOpen(slug);
      } else {
        await declineInvitation(slug);
        window.location.reload();
      }
    } catch {
      setError(t("invitations.failed"));
      setBusy(null);
    }
  }

  return (
    <div
      className="rounded-lg border border-blue-200 bg-blue-50/60 p-4 space-y-3"
      data-testid="pending-invitations"
    >
      <p className="text-sm font-medium text-zinc-900">{t("invitations.title")}</p>
      <ul className="space-y-2">
        {invitations.map((invitation) => (
          <li
            key={invitation.slug}
            className="flex flex-wrap items-center justify-between gap-2"
          >
            <span className="text-sm text-zinc-700">
              {t("invitations.inviteLine", {
                org: invitation.display_name,
                role: invitation.role,
              })}
            </span>
            <span className="flex gap-2">
              <button
                type="button"
                onClick={() => run(invitation.slug, "join")}
                disabled={busy !== null}
                className="px-3 py-1.5 text-xs font-medium rounded-md bg-zinc-900 text-white hover:bg-zinc-800 disabled:opacity-50 transition-colors"
              >
                {busy?.slug === invitation.slug && busy.action === "join"
                  ? t("invitations.joining")
                  : t("invitations.join")}
              </button>
              <button
                type="button"
                onClick={() => run(invitation.slug, "decline")}
                disabled={busy !== null}
                className="px-3 py-1.5 text-xs font-medium rounded-md border border-zinc-300 text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 transition-colors"
              >
                {busy?.slug === invitation.slug && busy.action === "decline"
                  ? t("invitations.declining")
                  : t("invitations.decline")}
              </button>
            </span>
          </li>
        ))}
      </ul>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}
