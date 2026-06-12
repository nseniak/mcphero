import { apiFetch } from "./client";
import type {
  AddUpstreamRequest,
  AddUserRequest,
  AdminUserInfo,
  AuditSearchResponse,
  ConnectResponse,
  ImportConfirmEntry,
  ImportPreviewResponse,
  ImportResultResponse,
  RoleAccessInfo,
  RoleSummary,
  ToolInfo,
  UpdateUpstreamRequest,
  UpstreamDetail,
  UpstreamSummary,
} from "./types";

export function fetchUpstreams(): Promise<UpstreamSummary[]> {
  return apiFetch<UpstreamSummary[]>("/api/admin/upstreams");
}

export function refreshUpstreamStatus(): Promise<UpstreamSummary[]> {
  return apiFetch<UpstreamSummary[]>("/api/admin/upstreams/refresh-status", {
    method: "POST",
  });
}

export function fetchUpstream(id: string): Promise<UpstreamDetail> {
  return apiFetch<UpstreamDetail>(`/api/admin/upstreams/${id}`);
}

export function fetchUpstreamLogs(id: string): Promise<{ logs: string | null }> {
  return apiFetch<{ logs: string | null }>(`/api/admin/upstreams/${id}/logs`);
}

export function fetchUpstreamTools(id: string): Promise<ToolInfo[]> {
  return apiFetch<ToolInfo[]>(`/api/admin/upstreams/${id}/tools`);
}

export function refreshUpstreamTools(id: string): Promise<ConnectResponse> {
  return apiFetch<ConnectResponse>(
    `/api/admin/upstreams/${id}/refresh-tools`,
    { method: "POST" },
  );
}

export function fetchAllTools(): Promise<ToolInfo[]> {
  return apiFetch<ToolInfo[]>("/api/admin/tools");
}

interface AdminMcpToolRaw {
  name: string;
  description: string | null;
  input_schema: Record<string, unknown>;
  annotations?: Record<string, unknown> | null;
  category: string;
}

export interface AdminMcpToolCategory {
  category: string;
  tools: ToolInfo[];
}

export async function fetchAdminMcpTools(): Promise<AdminMcpToolCategory[]> {
  const raw = await apiFetch<AdminMcpToolRaw[]>("/api/admin/admin-mcp/tools");
  const categoryMap = new Map<string, ToolInfo[]>();
  for (const t of raw) {
    const tool: ToolInfo = {
      upstream_id: "admin-mcp",
      original_name: t.name,
      prefixed_name: t.name,
      description: t.description,
      input_schema: t.input_schema,
      annotations: t.annotations as ToolInfo["annotations"] ?? null,
    };
    const list = categoryMap.get(t.category);
    if (list) {
      list.push(tool);
    } else {
      categoryMap.set(t.category, [tool]);
    }
  }
  return Array.from(categoryMap, ([category, tools]) => ({ category, tools }));
}

export function searchAudit(params: {
  user_id?: string;
  mcp_id?: string;
  tool?: string;
  action?: string;
  limit?: number;
  offset?: number;
}): Promise<AuditSearchResponse> {
  const qs = new URLSearchParams();
  if (params.user_id) qs.set("user_id", params.user_id);
  if (params.mcp_id) qs.set("mcp_id", params.mcp_id);
  if (params.tool) qs.set("tool", params.tool);
  if (params.action) qs.set("action", params.action);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  return apiFetch<AuditSearchResponse>(`/api/admin/audit?${qs}`);
}

export interface AuditFilterValues {
  user_ids: string[];
  upstream_ids: string[];
}

export function fetchAuditFilters(): Promise<AuditFilterValues> {
  return apiFetch<AuditFilterValues>("/api/admin/audit/filters");
}

export function fetchUsers(): Promise<AdminUserInfo[]> {
  return apiFetch<AdminUserInfo[]>("/api/admin/users");
}

export function fetchRoles(): Promise<RoleSummary[]> {
  return apiFetch<RoleSummary[]>("/api/admin/roles");
}

export interface StartupStatus {
  ready: boolean;
  total: number;
  connected: number;
  failed: number;
}

export function fetchStartupStatus(): Promise<StartupStatus> {
  return apiFetch<StartupStatus>("/api/admin/startup-status");
}

export function previewImport(data: Record<string, unknown>): Promise<ImportPreviewResponse> {
  return apiFetch<ImportPreviewResponse>("/api/admin/upstreams/import/preview", {
    method: "POST",
    body: JSON.stringify({ data }),
  });
}

export function confirmImport(
  data: Record<string, unknown>,
  entries: ImportConfirmEntry[],
): Promise<ImportResultResponse> {
  return apiFetch<ImportResultResponse>("/api/admin/upstreams/import/confirm", {
    method: "POST",
    body: JSON.stringify({ data, entries }),
  });
}

// --- Write operations ---

export function addUpstream(
  body: AddUpstreamRequest,
): Promise<UpstreamSummary> {
  return apiFetch<UpstreamSummary>("/api/admin/upstreams", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateUpstream(
  id: string,
  body: UpdateUpstreamRequest,
): Promise<UpstreamDetail> {
  return apiFetch<UpstreamDetail>(`/api/admin/upstreams/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function removeUpstream(id: string): Promise<void> {
  return apiFetch<void>(`/api/admin/upstreams/${id}`, {
    method: "DELETE",
  });
}

export function connectUpstream(id: string): Promise<ConnectResponse> {
  return apiFetch<ConnectResponse>(
    `/api/admin/upstreams/${id}/connect`,
    { method: "POST" },
  );
}

export function reconnectUpstream(id: string): Promise<ConnectResponse> {
  return apiFetch<ConnectResponse>(
    `/api/admin/upstreams/${id}/reconnect`,
    { method: "POST" },
  );
}

export function disconnectUpstream(id: string): Promise<void> {
  return apiFetch<void>(
    `/api/admin/upstreams/${id}/disconnect`,
    { method: "POST" },
  );
}

export function addUser(body: AddUserRequest): Promise<AdminUserInfo> {
  return apiFetch<AdminUserInfo>("/api/admin/users", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function setUserRole(
  email: string,
  role: string,
): Promise<AdminUserInfo> {
  return apiFetch<AdminUserInfo>(
    `/api/admin/users/${email}/role`,
    {
      method: "PUT",
      body: JSON.stringify({ role }),
    },
  );
}

export function removeUser(email: string): Promise<void> {
  return apiFetch<void>(`/api/admin/users/${email}`, {
    method: "DELETE",
  });
}

// --- Service tokens ---

export interface ServiceTokenInfo {
  label: string;
  role: string;
  created_by: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ServiceTokenCreateResponse {
  token: string;
  info: ServiceTokenInfo;
}

export function listServiceTokens(): Promise<ServiceTokenInfo[]> {
  return apiFetch<ServiceTokenInfo[]>("/api/admin/service-tokens");
}

export function createServiceToken(
  label: string,
  role: string,
): Promise<ServiceTokenCreateResponse> {
  return apiFetch<ServiceTokenCreateResponse>("/api/admin/service-tokens", {
    method: "POST",
    body: JSON.stringify({ label, role }),
  });
}

export function revokeServiceToken(label: string): Promise<void> {
  return apiFetch<void>(
    `/api/admin/service-tokens/${encodeURIComponent(label)}`,
    { method: "DELETE" },
  );
}

export function fetchRoleAccess(): Promise<RoleAccessInfo[]> {
  return apiFetch<RoleAccessInfo[]>("/api/admin/roles/access");
}

export function createRole(
  name: string,
  copyFrom?: string,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>("/api/admin/roles", {
    method: "POST",
    body: JSON.stringify({ name, copy_from: copyFrom || null }),
  });
}

export function deleteRole(name: string): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/api/admin/roles/${name}`, {
    method: "DELETE",
  });
}

export function renameRole(
  name: string,
  newName: string,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(`/api/admin/roles/${name}/rename`, {
    method: "PUT",
    body: JSON.stringify({ new_name: newName }),
  });
}

export function setRoleAutoEnableNew(
  roleName: string,
  autoEnableNew: boolean,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/auto-enable-new`,
    {
      method: "PUT",
      body: JSON.stringify({ auto_enable_new: autoEnableNew }),
    },
  );
}

export function setRoleMcpAccessEntry(
  roleName: string,
  mcpId: string,
  enabled: boolean,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/mcps/${mcpId}`,
    {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    },
  );
}

// --- Tool access ---

export function setRoleToolAccessEntry(
  roleName: string,
  upstreamId: string,
  toolName: string,
  enabled: boolean,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/tools/${toolName}`,
    { method: "PUT", body: JSON.stringify({ enabled }) },
  );
}

export function removeRoleToolAccessEntry(
  roleName: string,
  upstreamId: string,
  toolName: string,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/tools/${toolName}`,
    { method: "DELETE" },
  );
}

export function setRoleToolFallbackEnabled(
  roleName: string,
  upstreamId: string,
  fallbackEnabled: boolean | null,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/tool-fallback-enabled`,
    { method: "PUT", body: JSON.stringify({ fallback_enabled: fallbackEnabled }) },
  );
}

export function setRoleCategoryDefault(
  roleName: string,
  upstreamId: string,
  category: string,
  enabled: boolean,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/category-defaults/${category}`,
    { method: "PUT", body: JSON.stringify({ enabled }) },
  );
}

export function removeRoleCategoryDefault(
  roleName: string,
  upstreamId: string,
  category: string,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/category-defaults/${category}`,
    { method: "DELETE" },
  );
}

export function setRoleArgumentConstraint(
  roleName: string,
  upstreamId: string,
  toolName: string,
  argName: string,
  pattern: string,
  mode: "allow" | "forbid",
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/tools/${toolName}/constraints/${argName}`,
    { method: "PUT", body: JSON.stringify({ pattern, mode }) },
  );
}

export function removeRoleArgumentConstraint(
  roleName: string,
  upstreamId: string,
  toolName: string,
  argName: string,
): Promise<RoleAccessInfo> {
  return apiFetch<RoleAccessInfo>(
    `/api/admin/roles/${roleName}/upstreams/${upstreamId}/tools/${toolName}/constraints/${argName}`,
    { method: "DELETE" },
  );
}

export function disconnectGatewayUser(email: string): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(
    `/api/admin/gateway/users/${encodeURIComponent(email)}`,
    { method: "DELETE" },
  );
}
