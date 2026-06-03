# Admin MCP

The **Admin MCP** lets you manage MCP Hero from your AI assistant — you describe the change in natural language, your assistant calls the right management tool. It's an alternative surface to the dashboard, not a replacement.

## When to use it

The dashboard is faster for visual tasks (clicking through roles, scanning the audit log). Reach for the Admin MCP when:

- You'd rather describe the change than hunt for it ("disable destructive tools on Mixpanel for the user role")
- You want to do something bulk across many MCPs, roles, or tools
- You want to script setup ("create a `bi-team` role with read-only access to Mixpanel, MongoDB, and Snowflake")
- You're already in your AI client and don't want to switch tabs

Both surfaces talk to the same API, so changes made in one show up immediately in the other.

## Connecting your AI client

Open the **Admin MCP** page in the sidebar.

1. Copy the **Admin MCP server URL** at the top of the page (it ends in `/admin-mcp`).
2. Add it as a custom MCP to your AI client. The flow is the same as for the gateway MCP — see step 3 of [Getting started](getting-started.md). Use the URL on this page, *not* the gateway URL.
3. Sign in with the same Google account you use for the dashboard.

Once connected, your AI client sees the Admin MCP's management tools alongside its other MCP tools. Ask it `what admin tools do you have?` to confirm.

## What you can do

The Admin MCP exposes management tools across six areas. Open the Admin MCP page in the dashboard for the full, current list — every tool's name, description, and annotations are shown there, and that page stays in sync as MCP Hero ships new tools.

The categories at time of writing:

- **Upstream management** — list, add, remove, connect, disconnect, refresh tools.
- **Tool customization** — view/set/remove default arguments per upstream tool.
- **Audit** — search the audit log with filters, including arguments and policy decisions that aren't shown in the dashboard table.
- **User management** — list members, add or remove members, change a member's role.
- **Role management** — list, create, delete, rename roles.
- **Access policies** — set or clear every access rule a role can have: per-MCP, per-tool, annotation-based, argument checks.

## Authentication

Only members with an admin role can connect. If a non-admin tries, the connection is refused.

The same Google sign-in flow that secures the dashboard secures the Admin MCP — there's no separate token to manage. If your role changes from admin to something else, your existing Admin MCP session stops working as soon as the role change is saved.
