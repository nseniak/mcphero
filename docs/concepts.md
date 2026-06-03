# Concepts

Terms used throughout this documentation.

## MCP Hero

A gateway between your team's AI assistants and the MCP servers they use. You add MCPs once; your team connects their AI client to MCP Hero and gets every MCP you've approved, scoped to their role.

## Organization

The container for your team, your upstream MCPs, your roles, and your audit log. One MCP Hero account can belong to multiple organizations — switch between them via the **Manage organizations** link next to the organization name at the top of the page.

## Upstream MCP

An MCP server you've added to MCP Hero — for example Mixpanel, HubSpot, Stripe, MongoDB, or your own internal MCP. MCP Hero connects to it and proxies its tools to your team.

> **HTTP MCP vs stdio MCP**
>
> Upstream MCPs come in two kinds:
>
> - **HTTP MCP** (also called *remote* or *streamable-http*) — runs at a URL on the internet. You add it by pasting the URL.
> - **stdio MCP** (also called *self-hosted* or *local*) — distributed as a CLI command, for example `npx -y @modelcontextprotocol/server-postgres postgresql://warehouse.internal/analytics`. MCP Hero runs the command for you in a hosted sandbox; nobody on your team installs anything.
>
> Most hosted SaaS MCPs (Mixpanel, HubSpot, Stripe) are HTTP. MCPs that wrap an internal database or data warehouse are usually stdio.

## Gateway MCP

The single MCP server MCP Hero exposes to your team. Its URL ends in `/mcp/<your-org-slug>` — one URL per organization. You share it once; every team member pastes it into Claude, ChatGPT, Cursor, or another AI client. Their AI client sees one MCP — the gateway — but every tool call is forwarded to the right upstream MCP in your organization.

## Tool

An action exposed by an MCP — for example `query_events` (Mixpanel), `list_charges` (Stripe), `run_sql` (a database MCP). Roles can allow or deny access at the level of an entire MCP, an individual tool, or the arguments passed to a tool.

## Role

A named set of permissions that decides which MCPs and tools a member can use. MCP Hero ships with two roles — `admin` (full access plus management) and `user` (the default for new members) — and you can add your own.

## Team member

Someone in your organization, identified by their Google email. Each member has exactly one role. You add members from the Team page or by sharing your organization's invite link.

> **User authentication modes**
>
> When you add an upstream MCP, you pick how end users authenticate to it. The form labels these modes **None**, **Shared**, and **Per-user**.
>
> - **None** — users don't authenticate to this MCP at all. Either it's open, or its credentials live in the server config (a shared API key for a Mixpanel project, a Stripe restricted key, a MongoDB read-only user). Every member's call goes out with the same credentials.
> - **Shared** — *you* (or another admin) sign in once with OAuth on behalf of the team. Every member's call goes out as you. Use this for shared org resources like your company HubSpot or Linear workspace. Only one admin owns the connection at a time.
> - **Per-user** — every team member signs in personally with OAuth. Each call goes out as that member. Use this when the upstream is naturally scoped per person — a member's own GitHub repos or work calendar.
>
> **Stdio MCPs are locked to None.** Browser-based OAuth doesn't work inside a remote sandbox; stdio MCPs authenticate through the per-MCP Variables and Files surfaces described in [Upstream MCPs](upstream-mcps.md). The form hides the auth-mode picker for stdio. Full reasoning in [Stdio MCP authentication](stdio-authent.md).

## Admin MCP

A second MCP that MCP Hero exposes — at a URL ending in `/admin-mcp` — for administering MCP Hero itself. Connect your AI client to it and you can add upstreams, change roles, search the audit log, and so on conversationally. It's an alternative to clicking through the dashboard, not a replacement.
