/** Shared API response types matching the backend Pydantic models. */

/** Disconnect reasons that indicate an auth issue (vs a connection error). */
export const AUTH_DISCONNECT_REASONS = new Set([
  "token_expired",
  "token_refresh_failed",
]);

export type PlanName = "free" | "team";

export interface OrgMembership {
  slug: string;
  display_name: string;
  role: string;
  /** True when the user's role in this org is flagged
   *  ``is_admin=True``. Computed server-side from policy config —
   *  do NOT compare ``role`` against the literal string ``"admin"``,
   *  any role can be flagged admin. */
  is_admin: boolean;
  plan: PlanName;
}

/** Org list-row shape for the manage page — extends OrgMembership with
 *  per-org stats. Only ``/api/orgs`` populates these; ``/api/auth/me``
 *  returns the leaner OrgMembership. */
export interface OrgListEntry extends OrgMembership {
  member_count: number;
  http_upstream_count: number;
  stdio_upstream_count: number;
}

export interface CurrentOrgInfo {
  slug: string;
  display_name: string;
  role: string;
  /** Admin flag for the active org — see ``OrgMembership.is_admin``. */
  is_admin: boolean;
  plan: PlanName;
}

export interface UserInfo {
  email: string;
  roles: string[];
  is_admin: boolean;
  /** Instance-level superadmin (member of MCPOLIS_SUPERADMIN_EMAILS allowlist). */
  is_superadmin: boolean;
  orgs: OrgMembership[];
  current_org: CurrentOrgInfo | null;
}

export interface UpstreamSummary {
  id: string;
  display_name: string;
  transport: string;
  auth_mode: string;
  /** Org-level "this upstream is usable now". For service_account
   *  ⇔ shared session live. For both OAuth modes ⇔ at least one
   *  admin has authenticated. */
  ready: boolean;
  /** Email of the admin currently providing readiness for either
   *  OAuth mode, or null for service_account / when no admin is
   *  signed in. Drives the "Authenticated by alice@" + take-over
   *  UX uniformly across both OAuth modes. */
  slot_owner: string | null;
  tool_count: number;
  /** True while the post-connect catalog refresh (list_tools /
   *  list_resources / list_prompts) is running in the background.
   *  Drives the "Fetching info" indicator on the upstream row. */
  refreshing: boolean;
  /** True while a fire-and-forget admin reconnect task is in flight
   *  on the backend. Drives the disabled "Starting…" pill across
   *  tabs and refreshes — server truth, not local optimistic state.
   *  Survives ``busyAction`` clearing (the HTTP response returns in
   *  ms, but the underlying connect can take 1–60s for a sandbox
   *  cold pull). */
  starting: boolean;
  url: string | null;
  disconnect_reason: string | null;
}

export interface ConnectedUser {
  email: string;
  expires_at: string | null;
  is_admin: boolean;
}

export interface ServerInfo {
  name: string;
  version: string;
  title?: string | null;
}

export interface UpstreamDetail {
  id: string;
  display_name: string;
  transport: string;
  auth_mode: string;
  /** See UpstreamSummary.ready / slot_owner. */
  ready: boolean;
  slot_owner: string | null;
  /** See UpstreamSummary.starting — same fire-and-forget reconnect
   *  signal, surfaced on the detail page so the button + status pill
   *  show "Starting…" across tab switches and reloads. */
  starting: boolean;
  url: string | null;
  command: string | null;
  client_id: string | null;
  has_client_secret: boolean;
  oauth_app_domain: string | null;
  oauth_app_client_id: string | null;
  scopes: string[];
  default_arguments: Record<string, unknown>;
  server_config: Record<string, unknown>;
  server_info: ServerInfo | null;
  disconnect_reason: string | null;
  connected_users: ConnectedUser[];
  /** Per-MCP CPU/RAM/disk; ``null`` for HTTP upstreams. */
  sandbox_resources: SandboxResourcesView | null;
  /** ``true`` when the running session was started against a config
   *  / env-var snapshot that no longer matches the saved state.
   *  Drives the "stop & restart" dirty banner. Always false while
   *  the upstream isn't ready. */
  is_dirty: boolean;
  /** SHA-256 fingerprint of the saved config + env-var set as of
   *  this read. Frontend keys its per-MCP "Dismiss" sessionStorage
   *  entry by this value, so a fresh save automatically re-shows
   *  the banner even if the user dismissed the previous version. */
  config_hash: string | null;
}

/** One valid (cpu, ram, disk) combination the provider supports.
 *  Each entry maps to one row in the admin combined-picker dropdown.
 *  ``enabled`` reflects whether the active org's plan permits the
 *  combo; the dropdown still renders disabled options to make the
 *  set of "what you'd unlock on Team" visible. */
export interface SandboxResourceCombo {
  cpu_vcpus: number;
  memory_mb: number;
  disk_gb: number;
  enabled?: boolean;
}

/** Active sandbox provider's resource grid. Drives the per-MCP
 *  resource picker on the admin upstream form — the combined
 *  dropdown reads ``allowed_combinations`` so an off-grid (cpu, ram)
 *  pair can never be selected. The flat per-axis lists are still
 *  surfaced for callers that need them. */
export interface SandboxCapabilitiesResponse {
  provider: string;
  allowed_cpu_vcpus: number[];
  allowed_memory_mb: number[];
  /** Empty array ⇔ disk is fixed at template build time (E2B);
   *  the UI hides the disk axis from the combo label in that case. */
  allowed_disk_gb: number[];
  /** Authoritative list of valid (cpu, ram, disk) triples.
   *  Local-subprocess: the cross-product of the per-axis lists.
   *  E2B: the explicit template grid. */
  allowed_combinations: SandboxResourceCombo[];
  supports_pause_resume: boolean;
  supports_egress_filtering: boolean;
  supports_persistent_disk: boolean;
}

/** Per-MCP CPU/RAM/disk currently assigned to a stdio upstream. */
export interface SandboxResourcesView {
  cpu_vcpus: number;
  memory_mb: number;
  disk_gb: number;
  pids_limit: number | null;
  tmpfs_mb: number | null;
  /** Persistent volume opt-in. When true the active backend mounts
   *  a `/data` volume that survives across sessions; the UI control
   *  is rendered only when ``capabilities.supports_persistent_disk``
   *  is true. */
  persistent_disk_enabled: boolean;
}

export interface ToolAnnotationsInfo {
  title?: string | null;
  readOnlyHint?: boolean | null;
  destructiveHint?: boolean | null;
  idempotentHint?: boolean | null;
  openWorldHint?: boolean | null;
}

export interface ToolInfo {
  upstream_id: string;
  original_name: string;
  prefixed_name: string;
  description: string | null;
  input_schema: Record<string, unknown>;
  title?: string | null;
  output_schema?: Record<string, unknown> | null;
  annotations?: ToolAnnotationsInfo | null;
}

export interface AuditSearchResponse {
  entries: Record<string, unknown>[];
  count: number;
}

export interface UserToolSummary {
  name: string;
  description: string | null;
}

export interface UserMcpInfo {
  id: string;
  display_name: string;
  transport: string;
  auth_mode: string;
  /** Org-level Ready — same value the admin tab sees. For OAuth
   *  modes this means an admin has authenticated. The per-viewer
   *  signed-in state is reported separately as
   *  user_connection_status (only meaningful for per_user_oauth). */
  ready: boolean;
  url: string | null;
  user_connection_status: string;
  tool_count: number;
  tools: UserToolSummary[];
}

export interface ConnectResponse {
  authorization_url: string | null;
  connected: boolean;
  error: string | null;
  /** Fire-and-forget reconnect outcome. ``"pending"`` means the
   *  backend kicked off a detached connect task — stop waiting on
   *  this response. The button + status pill rebuild "Starting…"
   *  state from ``UpstreamSummary.starting`` (server truth) until
   *  the next ``policy_changed`` event signals the catalog refresh
   *  has completed and the upstream is Ready. Unset on the OAuth
   *  flow (which still uses ``authorization_url`` for its async
   *  branch). */
  outcome?: string | null;
  upstream_id?: string | null;
}

// --- New Phase 4b types ---

export interface AdminUserInfo {
  email: string;
  role: string;
  is_admin: boolean;
  status: "active" | "pending";
}

export interface RoleSummary {
  name: string;
  is_admin: boolean;
  is_default: boolean;
  user_count: number;
  service_token_count: number;
}

export interface McpAccessConfig {
  auto_enable_new: boolean;
  mcps: Record<string, boolean>;
}

export interface ToolAccessConfig {
  fallback_enabled: boolean | null;
  category_defaults: Record<string, boolean>;
  tools: Record<string, boolean>;
}

export interface ArgumentConstraint {
  pattern: string;
  mode: "allow" | "forbid";
}

export interface RoleAccessInfo {
  name: string;
  is_admin: boolean;
  is_default: boolean;
  mcp_access: McpAccessConfig;
  tool_access: Record<string, ToolAccessConfig>;
  argument_constraints: Record<string, Record<string, ArgumentConstraint>>;
}

export interface AddUpstreamRequest {
  id: string;
  display_name: string;
  // HTTP transport
  url?: string;
  headers?: Record<string, string>;
  // Stdio transport
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  // Stdio sandbox resources (optional; backend defaults apply when omitted)
  cpu_vcpus?: number;
  memory_mb?: number;
  disk_gb?: number;
  pids_limit?: number | null;
  tmpfs_mb?: number | null;
  persistent_disk_enabled?: boolean;
  // Auth
  auth_mode: string;
  auth_token?: string;
  client_id?: string;
  client_secret?: string;
  scopes?: string[];
  // Per-MCP env vars the wizard collected before the create call.
  // Each entry carries the value + an ``is_secret`` flag; secret
  // values are masked in the UI after save, plain values stay visible.
  template_vars?: Record<string, AddUpstreamTemplateVarSpec>;
}

/** Per-entry shape inside ``AddUpstreamRequest.template_vars``. */
export interface AddUpstreamTemplateVarSpec {
  value: string;
  is_secret: boolean;
}

/** Wire-shape of a per-MCP env-var summary.
 *
 * For secret rows, ``value`` is always ``null`` — the UI renders a
 * masked display from ``last_four``. For plain rows, ``value`` carries
 * the plaintext so the UI can render it verbatim.
 */
export interface TemplateVarSummary {
  name: string;
  is_secret: boolean;
  /** Plaintext value — present only when ``is_secret=false``. */
  value: string | null;
  /** Last 4 chars when the saved value is longer than 16 chars; null otherwise. */
  last_four: string | null;
  created_at: string;
  updated_at: string;
}

/** Wire-shape of a per-MCP Sandbox file summary.
 *
 * ``name`` is the URL-safe storage key (the path component on
 * ``PUT /api/admin/upstreams/{id}/sandbox-files/{name}``).
 * ``display_name`` is the free-form label rendered in the listing.
 *
 * Never carries ``contents`` — the upload page only displays
 * metadata (size + sha256 + last-modified). File names live in
 * their own namespace (no ``${...}`` substitution).
 */
export interface SandboxFileSummary {
  /** URL-safe storage key. */
  name: string;
  /** Free-form human label rendered in the listing. Falls back to
   *  ``name`` for legacy rows that pre-date this field. */
  display_name: string;
  /** Path inside the sandbox where the launcher writes the file.
   *  Accepts the same ``${...}`` references env-var values do
   *  (system + user Variables); resolved at launch. */
  target_path: string;
  /** Hex-encoded SHA-256 of the contents. Truncate for display. */
  sha256: string;
  size_bytes: number;
  created_at: string;
  updated_at: string;
}

/** Read-only system Variable row (``${HOME}``, …).
 *
 * Sourced from the live sandbox env at launch — surfaced in the
 * Variables list with a "system" badge so operators can discover
 * + reference them, but the value is not editable. */
export interface SystemVariable {
  name: string;
  value: string;
}

/** Per-entry shape inside ``UpdateUpstreamRequest.template_var_changes.sets``.
 *
 * Mirrors :class:`AddUpstreamTemplateVarSpec` so the buffered create-wizard
 * payload and the deferred edit-page payload share a wire shape. */
export interface UpdateUpstreamTemplateVarSpec {
  value: string;
  is_secret: boolean;
}

/** Buffered env-var mutations the deferred Edit/Save flow flushes
 *  in the same request that updates the upstream config. */
export interface UpdateUpstreamTemplateVarChanges {
  sets: Record<string, UpdateUpstreamTemplateVarSpec>;
  deletes: string[];
}

/** Patch shape for ``UpdateUpstreamRequest.sandbox_resources``.
 *
 * Each field is independently optional — ``undefined`` means
 * "leave the current value alone." The backend revalidates the
 * resulting combo against the active provider's grid and rejects
 * with a 400 + ``{message, field, value}`` so the form can flag
 * the offending control. */
export interface UpdateSandboxResourcesPatch {
  cpu_vcpus?: number;
  memory_mb?: number;
  disk_gb?: number;
  pids_limit?: number | null;
  tmpfs_mb?: number | null;
  persistent_disk_enabled?: boolean;
}

export interface UpdateUpstreamRequest {
  display_name?: string;
  auth_mode?: string;
  client_id?: string;
  client_secret?: string;
  server_config?: Record<string, unknown>;
  template_var_changes?: UpdateUpstreamTemplateVarChanges;
  sandbox_resources?: UpdateSandboxResourcesPatch;
}

export interface ImportDuplicateRef {
  proposed_id: string;
  group_label: string;
}

export interface ImportEntry {
  scope: string; // "project" | "user" | "standard"
  project_path: string | null;
  group_label: string;
  original_id: string;
  proposed_id: string;
  display_name: string;
  transport: string;
  auth_mode: string;
  blocked: boolean;
  blocked_reason: string | null;
  duplicate_of: ImportDuplicateRef | null;
}

export interface ImportPreviewResponse {
  entries: ImportEntry[];
  existing_ids: string[];
  parse_errors: string[];
}

export interface ImportConfirmEntry {
  scope: string;
  project_path: string | null;
  original_id: string;
  target_id: string;
}

export interface ImportErrorDetail {
  id: string;
  error: string;
}

export interface ImportResultResponse {
  added: string[];
  skipped: string[];
  errors: ImportErrorDetail[];
}

export interface AddUserRequest {
  email: string;
  role?: string;
}

export interface GatewayConfig {
  url: string;
  connected_users: string[];
  all_users: string[];
}

// --- Superadmin dashboard ---

export interface SuperadminOverviewCounts {
  orgs: number;
  users: number;
  upstreams: number;
  upstreams_connected: number;
  runtimes_loaded: number;
}

export interface SuperadminOverviewSystem {
  mode: "standalone" | "cloud";
  sandbox_runner_configured: boolean;
  sandbox_runner_url_count: number;
  mixpanel_enabled: boolean;
  sentry_enabled: boolean;
}

export interface SuperadminOverviewResponse {
  counts: SuperadminOverviewCounts;
  system: SuperadminOverviewSystem;
}

export interface SuperadminOrgListRow {
  id: string;
  slug: string;
  display_name: string;
  created_at: string;
  created_by_email: string | null;
  member_count: number;
  upstream_count: number;
  upstream_connected: number;
  runtime_loaded: boolean;
  plan: PlanName;
}

export interface SuperadminOrgListResponse {
  orgs: SuperadminOrgListRow[];
}

export interface SuperadminSubscriptionUpdateResponse {
  org_id: string;
  plan: PlanName;
}

export interface SuperadminUserListRow {
  email: string;
  org_count: number;
  is_superadmin: boolean;
  roles: string[];
}

export interface SuperadminUserListResponse {
  users: SuperadminUserListRow[];
}

export interface SuperadminUserOrgMembership {
  org_id: string;
  org_slug: string;
  org_display_name: string;
  role: string;
  is_admin: boolean;
  joined_at: string;
}

export interface SuperadminUserDetailResponse {
  email: string;
  is_superadmin: boolean;
  memberships: SuperadminUserOrgMembership[];
}

export interface SuperadminUpstreamListRow {
  org_id: string;
  org_slug: string;
  org_display_name: string;
  upstream_id: string;
  display_name: string;
  transport: string;
  auth_mode: string;
  connected: boolean;
}

export interface SuperadminUpstreamListResponse {
  upstreams: SuperadminUpstreamListRow[];
  runtimes_loaded: number;
  runtimes_total: number;
}

export interface SuperadminAuditSearchResponse {
  entries: Record<string, unknown>[];
  count: number;
}

export interface SuperadminAuditAggregatesTopRow {
  key: string;
  count: number;
}

export interface SuperadminAuditAggregatesResponse {
  sample_size: number;
  top_tools: SuperadminAuditAggregatesTopRow[];
  top_orgs: SuperadminAuditAggregatesTopRow[];
  top_deny_rules: SuperadminAuditAggregatesTopRow[];
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
}

export interface SuperadminAuthHealthRow {
  org_id: string;
  org_slug: string;
  upstream_id: string;
  user_id: string;
  is_admin_token: boolean;
  expires_at: string | null;
  refresh_failures: number;
  last_failure_at: string | null;
  expired: boolean;
  expiring_soon: boolean;
}

export interface SuperadminAuthHealthConnectionError {
  org_id: string;
  org_slug: string;
  upstream_id: string;
  error: string;
}

export interface SuperadminAuthHealthResponse {
  runtimes_loaded: number;
  runtimes_total: number;
  total_tokens: number;
  expired: number;
  expiring_soon: number;
  failed_refresh: number;
  rows: SuperadminAuthHealthRow[];
  connection_errors: SuperadminAuthHealthConnectionError[];
}

export interface SuperadminSystemBackendInfo {
  mode: "standalone" | "cloud";
  release: string;
  sentry_dsn_set: boolean;
  sentry_environment: string;
  mixpanel_token_set: boolean;
  mixpanel_api_host: string;
  mongo_uri_set: boolean;
  mongo_db_name: string;
  redis_url_set: boolean;
  encryption_key_set: boolean;
  encryption_key_fingerprint: string;
  test_mode: boolean;
  allow_stdio_mcp: boolean;
}

export interface SuperadminSystemResponse {
  backend: SuperadminSystemBackendInfo;
}

export interface SuperadminSessionsRevokedResponse {
  email: string;
  gateway_tokens_revoked: number;
  upstream_sessions_terminated: number;
  orgs_touched: number;
}

export interface SuperadminReauthResponse {
  email: string;
  org_id: string;
  upstream_id: string;
  cleared: boolean;
}
