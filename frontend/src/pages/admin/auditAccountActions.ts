/** Account actions recorded in the audit log: an org admin's changes to
 *  the org (teammates, MCPs, roles, service tokens, a member disconnected
 *  from the gateway), or an MCP Hero operator acting on the org.
 *  ``user_id`` is who acted, ``target_user_id`` the teammate it was done
 *  to, ``upstream_id`` the MCP, ``detail`` the role or token. Shared by
 *  the org Audit page and the operator's cross-org Audit page. */
export const ACCOUNT_ACTIONS = [
  "member_invited",
  "member_role_changed",
  "member_removed",
  "gateway_sign_in_revoked",
  "upstream_added",
  "upstream_removed",
  "role_created",
  "role_renamed",
  "role_deleted",
  "service_token_created",
  "service_token_revoked",
  "operator_sign_out_everywhere",
  "operator_clear_sign_in",
  "operator_plan_change",
] as const;

export type AccountAction = (typeof ACCOUNT_ACTIONS)[number];

export function isAccountAction(action: string): action is AccountAction {
  return (ACCOUNT_ACTIONS as readonly string[]).includes(action);
}
