import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import {
  BACKEND_URL as BACKEND,
  TEST_MCP_URL,
  apiLoginAs,
  createOrg,
  joinAs,
  makeMcpClientAtPath,
  mintMcpToken,
} from "./helpers";

/**
 * Gateway tool-call rate limits, end to end: real backend, real Redis
 * counters, the production Free-plan numbers, through the org's own
 * gateway URL (``/mcp/{slug}/``, the one the dashboard hands out).
 *
 * The spec makes its own Free org so its counters can't be touched by
 * other specs on the shard (counters are per org and per caller), and
 * no other spec loses quota to it. The e2e harness raises only the
 * per-IP / per-user dashboard and sign-in limits (all its traffic
 * comes from 127.0.0.1); tool-call limits stay at their plan values.
 */

const stamp = Date.now().toString(36);
const ORG = `rate-limit-${stamp}`;
const OWNER = `rl-owner-${stamp}@example.com`;
const TEAMMATE = `rl-teammate-${stamp}@example.com`;
const THIRD = `rl-third-${stamp}@example.com`;
const UPSTREAM_ID = "rl-tools";
// On the org-scoped URL tools are named {upstream}__{tool}.
const ECHO = `${UPSTREAM_ID}__echo`;
// Mirror FREE in backend/src/mcpolis/domain/services/plan_policy.py.
const FREE_CALLS_PER_MIN_PER_CALLER = 60;
const FREE_CALLS_PER_MIN_PER_ORG = 120;

async function asOwner(request: APIRequestContext): Promise<APIRequestContext> {
  await apiLoginAs(request, OWNER);
  const resp = await request.post(`${BACKEND}/api/orgs/${ORG}/switch`);
  expect(resp.status()).toBe(204);
  return request;
}

function textOf(result: CallToolResult): string {
  const first = result.content[0];
  return first?.type === "text" ? first.text : JSON.stringify(result.content);
}

async function echo(client: Client, message: string): Promise<CallToolResult> {
  return (await client.callTool({
    name: ECHO,
    arguments: { message },
  })) as CallToolResult;
}

test.describe("Gateway tool-call rate limit", () => {
  const clients: Client[] = [];

  async function connect(request: APIRequestContext, email: string): Promise<Client> {
    const token = await mintMcpToken(request, email, ORG);
    const client = await makeMcpClientAtPath(token, `mcp/${ORG}/`);
    clients.push(client);
    return client;
  }

  async function echoAll(client: Client, count: number, label: string): Promise<void> {
    for (let i = 0; i < count; i++) {
      const result = await echo(client, `${label} ${i}`);
      expect(result.isError ?? false, textOf(result)).toBe(false);
      expect(textOf(result)).toContain(`${label} ${i}`);
    }
  }

  test.beforeAll(async ({ request }) => {
    await createOrg(request, OWNER, ORG, "Rate Limit Spec");
    const api = await asOwner(request);
    const created = await api.post(`${BACKEND}/api/admin/upstreams`, {
      data: {
        id: UPSTREAM_ID,
        display_name: "Rate Limit Spec Upstream",
        url: `${TEST_MCP_URL}/mcp`,
        auth_mode: "service_account",
      },
    });
    expect([200, 201]).toContain(created.status());
    const started = await api.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`,
    );
    expect(started.status()).toBe(200);
    for (const email of [TEAMMATE, THIRD]) {
      const added = await api.post(`${BACKEND}/api/admin/users`, {
        // admin, so each member sees the new upstream whatever the
        // default role's auto-enable setting is.
        data: { email, role: "admin" },
      });
      expect([200, 201]).toContain(added.status());
    }
    // Each invitee accepts, which is what makes them a member.
    for (const email of [TEAMMATE, THIRD]) {
      await joinAs(request, email, ORG);
    }
  });

  test.afterAll(async ({ request }) => {
    for (const client of clients) {
      await client.close().catch(() => {});
    }
    const api = await asOwner(request);
    await api.delete(`${BACKEND}/api/orgs/${ORG}`);
  });

  test("each caller and then the whole Free org get a tool error naming the wait", async ({
    request,
  }) => {
    // One caller over its own limit.
    const owner = await connect(request, OWNER);
    await echoAll(owner, FREE_CALLS_PER_MIN_PER_CALLER, "owner");
    const ownerRefused = await echo(owner, "one too many");
    expect(ownerRefused.isError).toBe(true);
    expect(textOf(ownerRefused)).toMatch(
      /^Rate limit reached: you have made too many tool calls in this organization in the last minute\. Try again in \d+ seconds?\. The Team plan has higher limits\.$/,
    );

    // The limit is per caller: a teammate still gets a full quota,
    // which brings the org to its own limit.
    const teammate = await connect(request, TEAMMATE);
    await echoAll(
      teammate,
      FREE_CALLS_PER_MIN_PER_ORG - FREE_CALLS_PER_MIN_PER_CALLER,
      "teammate",
    );

    // A third member, untouched so far, now meets the org-wide limit.
    const third = await connect(request, THIRD);
    const orgRefused = await echo(third, "org is full");
    expect(orgRefused.isError).toBe(true);
    expect(textOf(orgRefused)).toMatch(
      /^Rate limit reached: your organization has made too many tool calls in the last minute\. Try again in \d+ seconds?\. The Team plan has higher limits\.$/,
    );
  });
});
