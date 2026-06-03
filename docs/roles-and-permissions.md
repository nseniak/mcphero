# Roles and permissions

Everything you do on the **Roles & Permissions** page: managing roles, deciding which MCPs and tools each role can use, and constraining tool arguments.

If "role" is unfamiliar, skim [Concepts](concepts.md) first.

## The built-in roles

MCP Hero ships with two roles:

- **admin** — full management access (admin dashboard + Admin MCP) and access to every MCP.
- **user** — the default role new members are assigned. Has no special permissions; you decide which MCPs and tools it can use.

Each role has its own complete set of permissions. There's no inheritance — what you set on one role doesn't propagate to another.

## Creating a role

1. Click **Add role**.
2. Enter a **Name**.
3. Pick **Copy settings from** if you want to start from an existing role's permissions, or leave it on **None (empty role)** for a blank slate.
4. Click **Create**.

The new role appears as a tab next to admin and user.

## Renaming a role

1. Click the role's tab.
2. Click **Edit** in the **Settings** section.
3. Change the **Name** and click **Save**.

## Deleting a role

1. Click the role's tab.
2. Click **Edit** → **Delete**.
3. Confirm in the dialog.

A role with members assigned to it can't be deleted — reassign those members to another role first (see [Team](team.md)).

## Choosing which MCPs a role can access

Each role's **MCP Permissions** section has:

- **Enable new MCPs by default** — when On, every MCP you add in the future is automatically accessible to this role. When Off, future MCPs are blocked until you explicitly enable them here. This setting doesn't change anything about MCPs you've already added.
- **A row per existing MCP** — each with an **Enabled** toggle. On = role can use it. Off = role can't.

That's the MCP-level decision. Tool-level decisions (which tools *within* an MCP) come next.

## Choosing which tools a role can call

Click an MCP's name in the role's MCP Permissions section. You land on **{MCP} — Tool Permissions**, organized into three sections:

- **Read-only tools (N)** — tools the upstream marks as having no side effects (e.g. `query_events`, `list_charges`).
- **Destructive tools (N)** — tools the upstream marks as making big or irreversible changes (e.g. `delete_record`, `refund_charge`).
- **Other tools (N)** — tools without annotations.

Each section has a **3-state toggle**:

- **All on** — every tool in the section is allowed
- **All off** — every tool in the section is blocked
- **Per tool** — each tool follows its own toggle (the row-level switch below)

Below the section toggle, every tool has its own row with an individual toggle that overrides the section default.

> **Where do "Read-only" and "Destructive" come from?**
>
> They're hints set by the upstream MCP itself, baked into each tool's annotations. Some MCPs annotate every tool; some annotate none. Tools without annotations land in **Other tools**.
>
> Use the section toggles to make broad statements like "this role can use any read-only tool but no destructive ones." Use individual tool toggles to override that for specific cases.

## Restricting tool arguments

Even when a tool is allowed, you can require its arguments to match (or not match) a regex pattern. Use this to block destructive SQL, scope tool calls to a specific resource, or otherwise narrow what the AI can pass.

1. On a tool's permissions page, click the row to expand it. (Only tools with input arguments expand.)
2. Click **Edit** next to **Argument checks**.
3. For each argument you want to constrain, pick a mode and enter a pattern:
   - **Allow** — the argument's value must match the pattern.
   - **Forbid** — the argument's value must NOT match the pattern.
4. Click **Save**.

Argument checks use **Python regex syntax**.

Examples:

- Forbid `\b(DROP|DELETE|TRUNCATE)\b` on a SQL `query` argument to block destructive queries.
- Allow `^acme/` on a GitHub `repo` argument to scope calls to your `acme` org.
- Forbid `^https?://(?!internal\.example\.com)` on a `url` argument to keep an HTTP-fetcher tool inside your network.

If the argument check fails, the tool call is denied — the AI sees an error and the call is logged in the [audit log](audit-log.md) with the policy decision recorded.
