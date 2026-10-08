/**
 * An admin revoking a member's gateway sign-in (Team page →
 * "Disconnect from gateway", ``DELETE /api/admin/gateway/users/{email}``)
 * cuts off the member's LIVE gateway connection, not only their next
 * connection.
 *
 * Two things a connected client must see:
 *
 * 1. The next request on the same session is refused (401): the
 *    gateway checks the bearer on every request.
 * 2. The open server-to-client stream (the ``GET`` event stream an MCP
 *    client keeps open for notifications) is closed. Before the fix the
 *    revoke route left the session running, and even the session
 *    "terminate" helper only forgot the session id without closing it,
 *    so that stream stayed open and kept receiving the org's
 *    notifications.
 *
 * Co-location-safe: gateway tokens are per user across all orgs, so the
 * test revokes a member it creates for itself, never a shared seed user.
 */
import { test, expect } from "@playwright/test";

import {
  apiLoginAs,
  joinAs,
  mintMcpToken,
  makeMcpClientAtPath,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";
const PROTOCOL_VERSION = "2025-06-18";

/** Open a raw gateway session and its ``GET`` event stream. Returns
 *  the session id and a reader on the stream. */
async function openSessionWithStream(token: string) {
  const url = `${BACKEND}/mcp/${ORG}/`;
  const base = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  };
  const init = await fetch(url, {
    method: "POST",
    headers: base,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "e2e-revoke-stream", version: "0" },
      },
    }),
  });
  expect(init.status).toBe(200);
  const sessionId = init.headers.get("mcp-session-id");
  expect(sessionId, "gateway must assign a session id").toBeTruthy();
  await init.text();

  const withSession = {
    ...base,
    "mcp-session-id": sessionId as string,
    "mcp-protocol-version": PROTOCOL_VERSION,
  };
  const initialized = await fetch(url, {
    method: "POST",
    headers: withSession,
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    }),
  });
  expect(initialized.status).toBe(202);

  const stream = await fetch(url, {
    method: "GET",
    headers: { ...withSession, Accept: "text/event-stream" },
  });
  expect(stream.status).toBe(200);
  const reader = (stream.body as ReadableStream<Uint8Array>).getReader();
  return { url, withSession, reader };
}

/** Read until the stream ends or ``ms`` passes. True iff it ended. */
async function streamEndsWithin(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  ms: number,
): Promise<boolean> {
  const deadline = Date.now() + ms;
  for (;;) {
    const left = deadline - Date.now();
    if (left <= 0) return false;
    const next = await Promise.race([
      reader.read().then((r) => (r.done ? "ended" : "data")).catch(() => "ended"),
      new Promise<"timeout">((r) => setTimeout(() => r("timeout"), left)),
    ]);
    if (next === "ended") return true;
    if (next === "timeout") return false;
  }
}

test("revoking a member's gateway sign-in refuses their next call and closes their open stream", async ({
  request,
}) => {
  const member = `revoke-live-${Date.now()}@example.com`;
  await apiLoginAs(request, ADMIN);
  const added = await request.post(`${BACKEND}/api/admin/users`, {
    data: { email: member, role: "user" },
  });
  expect([200, 201]).toContain(added.status());
  // A member: they accepted the invitation.
  await joinAs(request, member, ORG);
  await apiLoginAs(request, ADMIN);
  const token = await mintMcpToken(request, member, ORG);

  // The member's MCP client works before the revoke.
  const client = await makeMcpClientAtPath(token, `mcp/${ORG}/`);
  const { url, withSession, reader } = await openSessionWithStream(token);
  try {
    const ok = await client.callTool({
      name: "test-tools__echo",
      arguments: { message: "before-revoke" },
    });
    expect(JSON.stringify(ok.content)).toContain("before-revoke");
    // The stream is open: nothing ends it on its own.
    expect(await streamEndsWithin(reader, 500)).toBe(false);

    const revoke = await request.delete(
      `${BACKEND}/api/admin/gateway/users/${encodeURIComponent(member)}`,
    );
    expect(revoke.status()).toBe(200);

    // 1. The next request on the same live session is refused.
    const after = await fetch(url, {
      method: "POST",
      headers: withSession,
      body: JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }),
    });
    expect(after.status).toBe(401);
    let refused = false;
    try {
      await client.callTool({
        name: "test-tools__echo",
        arguments: { message: "after-revoke" },
      });
    } catch {
      refused = true;
    }
    expect(refused).toBe(true);

    // 2. The open stream is closed by the server.
    expect(await streamEndsWithin(reader, 5_000)).toBe(true);
  } finally {
    await reader.cancel().catch(() => {});
    await client.close().catch(() => {});
    await request
      .delete(`${BACKEND}/api/admin/users/${encodeURIComponent(member)}`)
      .catch(() => {});
  }
});
