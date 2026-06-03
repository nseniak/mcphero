import type { UpstreamSummary, UpstreamDetail } from "../api/types";
import type { TranslationKey } from "../i18n/index";

type TFunction = (
  key: TranslationKey,
  params?: Record<string, string | number>,
) => string;

/** Compute the label override for a StatusBadge rendering an upstream
 *  row in the admin tab. Returns ``undefined`` to fall back to the
 *  StatusBadge's default text — that path covers:
 *    - ready=true with no custom slot owner ("Ready")
 *    - ready=false with a non-auth ``disconnect_reason`` ("Couldn't connect")
 *    - ready=false with an AUTH-class ``disconnect_reason`` like
 *      ``token_expired`` ("Disconnected", with the banner row showing
 *      the reason). The AUTH-class fall-through is deliberate: the
 *      pill stays terse while the banner carries the detail.
 *
 *  Otherwise we override the label to communicate the actual state:
 *    - ready=true with a slot owner who isn't the viewer ⇒
 *      "Ready, by alice@co.com"
 *    - ready=false on an OAuth-mode upstream ⇒ "Authentication needed"
 *    - ready=false on a stdio service_account ⇒ "Not started"
 *    - ready=false on an HTTP service_account ⇒ default "Disconnected"
 */
export function upstreamStatusLabel(
  u: UpstreamSummary | UpstreamDetail,
  viewerEmail: string | null,
  t: TFunction,
): string | undefined {
  if (u.ready) {
    if (u.slot_owner && u.slot_owner !== viewerEmail) {
      return t("status.connectedBy", { email: u.slot_owner });
    }
    return undefined;
  }
  if (u.disconnect_reason) {
    return undefined;
  }
  if (u.auth_mode === "admin_oauth" || u.auth_mode === "per_user_oauth") {
    return t("status.authenticationNeeded");
  }
  if (u.transport === "stdio") {
    // The granular sandbox lifecycle pill was removed along with the
    // runner-side state registry; the per-upstream status now reduces
    // to "ready / not ready" via the existing badges.
    return t("status.notStarted");
  }
  return undefined;
}
