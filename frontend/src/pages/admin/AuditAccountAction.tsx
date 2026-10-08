import { useTranslation } from "../../i18n/index";
import type { TranslationKey } from "../../i18n/index";
import { isAccountAction } from "./auditAccountActions";
import type { AccountAction } from "./auditAccountActions";

/** The line each account action reads as. Every action needs one. */
const ACCOUNT_ACTION_TEXT: Record<AccountAction, TranslationKey> = {
  member_invited: "audit.memberInvited",
  member_role_changed: "audit.memberRoleChanged",
  member_removed: "audit.memberRemoved",
  gateway_sign_in_revoked: "audit.gatewaySignInRevoked",
  upstream_added: "audit.upstreamAdded",
  upstream_removed: "audit.upstreamRemoved",
  role_created: "audit.roleCreated",
  role_renamed: "audit.roleRenamed",
  role_deleted: "audit.roleDeleted",
  service_token_created: "audit.serviceTokenCreated",
  service_token_revoked: "audit.serviceTokenRevoked",
  operator_sign_out_everywhere: "audit.operatorSignOutEverywhere",
  operator_clear_sign_in: "audit.operatorClearSignIn",
  operator_plan_change: "audit.operatorPlanChange",
};

/** One line saying what happened, or ``null`` for any other row. */
export function AccountActionText({ entry }: { entry: Record<string, unknown> }) {
  const { t } = useTranslation();
  const action = String(entry.action ?? "");
  if (!isAccountAction(action)) return null;
  const text = t(ACCOUNT_ACTION_TEXT[action], {
    target: String(entry.target_user_id ?? "—"),
    detail: String(entry.detail ?? "—"),
    upstream: String(entry.upstream_id || "—"),
  });
  return <span className="text-xs text-zinc-700">{text}</span>;
}

/** "MCP Hero operator" tag shown next to the actor of an operator row. */
export function OperatorTag({ entry }: { entry: Record<string, unknown> }) {
  const { t } = useTranslation();
  if (entry.actor_role !== "operator") return null;
  return (
    <span
      className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800"
      data-testid="audit-operator-tag"
    >
      {t("audit.operatorTag")}
    </span>
  );
}
