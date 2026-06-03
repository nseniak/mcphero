# Getting started

A short walkthrough that takes you from sign-up to your first MCP shared with your team.

You will:

1. Create your organization
2. Add your first upstream MCP
3. Connect your own AI client to the gateway
4. Invite a teammate

If any term in here is unfamiliar, see [Concepts](concepts.md).

## 1. Create your organization

Sign in to MCP Hero with Google. The first time you sign in, MCP Hero asks for an organization name — that's the container for your team, your MCPs, and your roles. The short name (used in URLs) auto-fills from the display name; both are editable.

Click **Create organization**. You will land on the **Upstream MCPs** page, ready to add your first one.

## 2. Add your first upstream MCP

Click **Add MCP**. The wizard has two steps.

**Step 1 — Configuration.** Pick how to connect:

- **URL** — paste the URL of an HTTP MCP (Mixpanel, HubSpot, Stripe, and so on).
- **JSON** — paste a standard `{"mcpServers": {…}}` config block. Useful if you already have one from another tool or a teammate, or for stdio MCPs (which are commands, not URLs).

When you paste JSON that uses `${NAME}` references, a **Variables** panel appears so you can fill those values inline. If MCP Hero spots a raw credential in the JSON, it offers to extract it into a Variable for you. Click **Next**.

**Step 2 — Details.** Pick an **ID** and **Display Name** (auto-filled from the URL or package). For HTTP MCPs, pick the auth mode the upstream uses (the picker is hidden for stdio — see below). For stdio MCPs, pick the sandbox **Resources** (CPU / memory / disk).

> **Which authentication mode?**
>
> See *User authentication modes* in [Concepts](concepts.md). Short version: pick **None** if the MCP doesn't need user-level auth (it's open, or you'll paste a shared API key into its server config), **Shared** if it's a workspace you'll sign into once on the team's behalf, or **Per-user** if every team member should sign in for themselves. Stdio MCPs are always **None** — they authenticate via Variables and Files instead (see [Upstream MCPs](upstream-mcps.md)).

Click **Add**. MCP Hero connects to the upstream and discovers its tools. The new MCP appears on the Upstream MCPs page.

Detailed flows for each connection style — including stdio MCPs and OAuth-protected MCPs — are in [Upstream MCPs](upstream-mcps.md). For credential-handling specifics on stdio MCPs, see [Stdio MCP authentication](stdio-authent.md).

## 3. Connect your own AI client to the gateway

Open **Connect AI Assistant** in the sidebar. Your gateway URL is shown on the page — it ends in `/mcp/<your-org-slug>`. That single URL is all your AI client needs. Through it, your client will see every upstream MCP you have access to in this organization.

Pick your client from the tabs and follow the on-page steps. MCP Hero ships built-in instructions for:

- Claude Code
- Claude.ai
- Claude Desktop
- Codex
- ChatGPT
- Cursor

> **My client is not listed**
>
> Any MCP client that speaks the Streamable HTTP transport will work. Use the gateway URL directly, or copy the JSON config from the **Advanced: JSON configuration** disclosure.

Once connected, ask your AI client to list its available tools — you should see your new upstream's tools, prefixed with the upstream name (for example `mixpanel__query_events`).

## 4. Invite a teammate

Open the **Team** page.

1. Click **Add Member**, enter their Google email, pick a role (it defaults to **user**), and save.
2. Copy the invite link at the top of the page and send it to them.

When they click the link and sign in with Google, they land directly in your organization with the role you assigned.

Members have to be added before they can join. Someone who signs in to MCP Hero with Google but hasn't been added to your team won't see your organization — they'll be prompted to create their own.

---

You now have an organization with one MCP, one teammate, and one AI client connected.

Next steps:

- Add more upstream MCPs — see [Upstream MCPs](upstream-mcps.md).
- Set up roles for different access levels — see [Roles and permissions](roles-and-permissions.md).
- Manage MCP Hero conversationally from your AI client — see [Admin MCP](admin-mcp.md).
