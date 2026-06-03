import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import { makeMcpClient, mintMcpToken, BACKEND_URL as BACKEND } from "./helpers";
const ORG_A = "acme-corp";
const ADMIN_A = "admin@example.com";

/**
 * End-to-end widget forwarding through the multi-org cloud gateway.
 *
 * The seeded ``test-tools`` upstream (now backed by the bundled demo
 * MCP server at ``backend/src/mcpolis/dev/demo_mcp_server.py``)
 * advertises five widget-opening tools with ``_meta.ui.resourceUri``
 * pointing at ``ui://mcp-demo/widget/<name>`` resources. The gateway
 * must:
 *
 *   1. Forward those tools with rewrapped widget URIs
 *      (``mcphero://...``) so the client's ``resources/read``
 *      round-trips through the same wrapping pipeline.
 *   2. Forward both ``_meta.ui.resourceUri`` and the legacy flat
 *      ``_meta["ui/resourceUri"]`` keys.
 *   3. Re-advertise the MCP-Apps capability extension
 *      (``io.modelcontextprotocol/ui``) in ``initialize``.
 *   4. Read the widget shell HTML with the exact MIME
 *      ``text/html;profile=mcp-app`` and forward the ``_meta.ui.csp``
 *      hints byte-for-byte.
 */

test.describe("Demo upstream widgets through the gateway", () => {
  let client: Client | null = null;

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("tools/list surfaces the five widget tools, rewrapped widget URIs", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const result = await client.listTools();
    const names = result.tools.map((t) => t.name);
    for (const widget of ["inline", "fullscreen", "pip", "counter", "solar_system"]) {
      const expected = `${ORG_A}__test-tools__open_${widget}_widget`;
      expect(names).toContain(expected);
    }

    // Each widget tool's ``_meta.ui.resourceUri`` must be a
    // gateway-wrapped URI baking in the org slug + upstream id.
    // Crucially the rewritten URI MUST keep the ``ui://`` scheme — the
    // MCP Apps spec validates that prefix and Inspector / Claude
    // reject anything else with "Invalid UI resource URI".
    const inline = result.tools.find(
      (t) => t.name === `${ORG_A}__test-tools__open_inline_widget`
    );
    expect(inline).toBeDefined();
    const meta = inline!._meta as
      | { ui?: { resourceUri?: string }; "ui/resourceUri"?: string }
      | undefined;
    expect(meta).toBeDefined();
    const nestedUri = meta?.ui?.resourceUri;
    expect(nestedUri).toBeDefined();
    expect(nestedUri!).toMatch(
      new RegExp(`^ui://mcphero/orgs/${ORG_A}/upstreams/test-tools/widgets/`)
    );
    // Legacy flat key is rewritten to the same wrapped URI.
    expect(meta?.["ui/resourceUri"]).toBe(nestedUri);
  });

  test("resources/read of a widget URI returns shell HTML with the exact MIME", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const tools = await client.listTools();
    const inline = tools.tools.find(
      (t) => t.name === `${ORG_A}__test-tools__open_inline_widget`
    );
    expect(inline).toBeDefined();
    const meta = inline!._meta as
      | { ui?: { resourceUri?: string } }
      | undefined;
    const wrappedUri = meta?.ui?.resourceUri;
    expect(wrappedUri).toBeDefined();

    const read = await client.readResource({ uri: wrappedUri! });
    expect(read.contents).toHaveLength(1);
    const item = read.contents[0] as {
      mimeType?: string;
      text?: string;
      _meta?: { ui?: { csp?: { resourceDomains?: string[] } } };
    };
    // FINDINGS §1.4: MIME must be the exact widget profile, not a
    // stripped ``text/html``.
    expect(item.mimeType).toBe("text/html;profile=mcp-app");
    // Shell HTML imports the real widget JS via dynamic import.
    expect(item.text).toContain("import(");
    expect(item.text).toContain("/dev/mcp-demo/widget/inline.js?t=");
    // FINDINGS §1.5: CSP ``resourceDomains`` survives the gateway
    // round-trip — without it the iframe can't load the SDK bundle.
    const cspDomains = item._meta?.ui?.csp?.resourceDomains ?? [];
    expect(cspDomains.some((d) => d.includes("unpkg.com"))).toBe(true);
  });

  test("prompts/list surfaces the demo's two prompts", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const result = await client.listPrompts();
    const names = result.prompts.map((p) => p.name);
    expect(names).toContain(`${ORG_A}__test-tools__greet_prompt`);
    expect(names).toContain(`${ORG_A}__test-tools__summarize_clicks_prompt`);

    // greet_prompt with the standard ``name`` argument.
    const out = await client.getPrompt({
      name: `${ORG_A}__test-tools__greet_prompt`,
      arguments: { name: "world" },
    });
    expect(out.messages.length).toBeGreaterThanOrEqual(1);
    const first = out.messages[0];
    expect(first.role).toBe("user");
    const content = first.content as { type: string; text?: string };
    expect(content.type).toBe("text");
    expect(content.text).toContain("world");
  });

  test("gateway initialize re-advertises capabilities.extensions[io.modelcontextprotocol/ui]", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const caps = client.getServerCapabilities() as
      | { extensions?: Record<string, { mimeTypes?: string[] }> }
      | undefined;
    expect(caps?.extensions).toBeDefined();
    const ui = caps!.extensions!["io.modelcontextprotocol/ui"];
    expect(ui).toBeDefined();
    expect(ui.mimeTypes).toContain("text/html;profile=mcp-app");
  });

  test("resources/list surfaces every widget shell with its CSP meta", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN_A, ORG_A);
    client = await makeMcpClient(token, ORG_A, "mcp");

    const list = await client.listResources();
    const names = list.resources.map((r) => r.name);
    for (const widget of [
      "inline-widget",
      "fullscreen-widget",
      "pip-widget",
      "counter-widget",
      "solar-widget",
    ]) {
      expect(names).toContain(`${ORG_A}__test-tools__${widget}`);
    }
    const inlineRes = list.resources.find(
      (r) => r.name === `${ORG_A}__test-tools__inline-widget`
    );
    expect(inlineRes).toBeDefined();
    const meta = inlineRes!._meta as
      | { ui?: { csp?: { resourceDomains?: string[] } } }
      | undefined;
    expect(meta?.ui?.csp).toBeDefined();
  });
});
