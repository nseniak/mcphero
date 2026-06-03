import { apiFetch } from "./client";
import type { ConnectResponse, GatewayConfig, UserMcpInfo } from "./types";

export function fetchUserMcps(): Promise<UserMcpInfo[]> {
  return apiFetch<UserMcpInfo[]>("/api/user/mcps");
}

export function fetchGatewayConfig(): Promise<GatewayConfig> {
  return apiFetch<GatewayConfig>("/api/config/gateway");
}

export function connectUpstream(upstreamId: string): Promise<ConnectResponse> {
  return apiFetch<ConnectResponse>(`/api/auth/connect/${upstreamId}`);
}

export function disconnectUpstream(upstreamId: string): Promise<void> {
  return apiFetch<void>(`/api/auth/disconnect/${upstreamId}`, {
    method: "POST",
  });
}
