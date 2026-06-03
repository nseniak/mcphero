import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import { makeMcpClient, mintMcpToken, apiLoginAs, BACKEND_URL as BACKEND } from "./helpers";
const ORG_A = "acme-corp";
const ORG_B = "beta-org";
const ADMIN_A = "admin@example.com";

/**
 * End-to-end resource + prompt forwarding through the multi-org cloud
 * gateway.  The seeded ``test-tools`` upstream (``tests/e2e/test_mcp_server.py``)
 * exposes:
 *  - one static resource ``test://hello-world`` returning "Hello, world!"
 *  - one prompt ``greet_prompt`` taking a ``name`` argument
 *  - a ``serverInfo``-shaped self-description string
 *
 * The gateway must (a) forward the resource list with org/upstream
 * prefixing, (b) round-trip a wrapped URI back through the upstream's
 * read_resource, (c) forward prompt list / get with the same prefixing,
 * and (d) refuse cross-org reads from a non-member.
 */

async function adminApi(request: APIRequestContext) {
  await apiLoginAs(request, ADMIN_A);
  return request;
}

test.describe("Gateway MCP resources + prompts", () => {
  let client: Client | null = null;

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("resources/list surfaces the seeded resource with the {org}__{upstream}__ prefix", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const result = await client.listResources();
    const names = result.resources.map((r) => r.name);
    expect(names).toContain(`${ORG_A}__test-tools__hello-world`);

    // The wrapped URI scheme bakes in org slug + upstream id.
    const seeded = result.resources.find(
      (r) => r.name === `${ORG_A}__test-tools__hello-world`
    );
    expect(seeded).toBeDefined();
    expect(seeded!.uri).toMatch(
      new RegExp(`^mcphero://orgs/${ORG_A}/upstreams/test-tools/resources/`)
    );
  });

  test("resources/read round-trips the wrapped URI and returns the upstream's payload", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const list = await client.listResources();
    const seeded = list.resources.find(
      (r) => r.name === `${ORG_A}__test-tools__hello-world`
    );
    expect(seeded).toBeDefined();

    const read = await client.readResource({ uri: seeded!.uri });
    expect(read.contents).toHaveLength(1);
    const first = read.contents[0] as { text?: string };
    expect(first.text).toBe("Hello, world!");
  });

  test("prompts/list surfaces the seeded prompt with the same prefix shape", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const result = await client.listPrompts();
    const names = result.prompts.map((p) => p.name);
    expect(names).toContain(`${ORG_A}__test-tools__greet_prompt`);
  });

  test("prompts/get forwards arguments unchanged to the upstream", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const result = await client.getPrompt({
      name: `${ORG_A}__test-tools__greet_prompt`,
      arguments: { name: "world" },
    });
    expect(result.messages).toHaveLength(1);
    const msg = result.messages[0];
    expect(msg.role).toBe("user");
    const content = msg.content as { type: string; text?: string };
    expect(content.type).toBe("text");
    expect(content.text).toContain("world");
  });

  test("gateway initialize instructions surface the upstream's self-description", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");
    const instructions = client.getInstructions();
    expect(instructions).toBeTruthy();
    // The seeded upstream's instructions text is folded into the
    // gateway's downstream ``initialize`` instructions block.
    // The demo upstream's instructions string surfaces inside the
    // gateway's "Connected upstreams" block (see _instructions_for_org_with_upstreams).
    expect(instructions).toContain("Demo upstream for the MCP Hero test suite");
  });

  test("gateway advertises resources.listChanged + prompts.listChanged in initialize", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");
    const caps = client.getServerCapabilities();
    expect(caps?.resources?.listChanged).toBe(true);
    expect(caps?.prompts?.listChanged).toBe(true);
  });

  test("resources/read for an org the user is not a member of is refused", async ({
    request,
  }) => {
    // alice-only is in ORG_A; she should not be able to forge a wrapped
    // URI naming ORG_B and read through it.  The gateway membership
    // guard on the wrapped URI's slug segment must catch it before
    // the upstream is touched.
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    // First, locate the legitimate wrapped URI to copy its base64
    // segment into a forged one.
    const list = await client.listResources();
    const legit = list.resources.find(
      (r) => r.name === `${ORG_A}__test-tools__hello-world`
    );
    expect(legit).toBeDefined();
    const base64Segment = String(legit!.uri).split("/").slice(-1)[0];
    const forgedUri =
      `mcphero://orgs/${ORG_B}/upstreams/test-tools/resources/${base64Segment}`;

    const read = await client.readResource({ uri: forgedUri });
    // The error is surfaced as a single TextResourceContents item — no
    // ``isError`` bit on resources/read.
    const text = (read.contents[0] as { text?: string }).text ?? "";
    expect(text.toLowerCase()).toContain("not a member");
  });
});
