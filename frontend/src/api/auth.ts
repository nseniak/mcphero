import { apiFetch } from "./client";
import type { UserInfo } from "./types";

export function fetchMe(): Promise<UserInfo> {
  return apiFetch<UserInfo>("/api/auth/me");
}

export function fetchAuthStatus(): Promise<{ has_users: boolean }> {
  return apiFetch<{ has_users: boolean }>("/api/auth/status");
}

export function logout(): Promise<void> {
  return apiFetch<void>("/api/auth/logout", { method: "POST" });
}

export function getLoginUrl(): string {
  return "/api/auth/login";
}
