# Operator access

MCP Hero stores credentials for the MCP servers your team connects, so it's fair to ask: what can the people who run MCP Hero actually see? This page answers that.

## The operator role

MCP Hero has an **operator** role (you may see it called `superadmin` in our open-source code). It's how the people running the hosted service keep it healthy: spotting an organization whose connections are failing, helping a member who's locked out, or removing an organization on request.

The operator role is an explicit list of email addresses, set when the service starts. On a self-hosted MCP Hero, that list is yours to control, so the only people with this access are the ones you name, or nobody if you leave it empty.

## What an operator can see

- Organization names, member lists, and which MCP servers you've connected, along with whether each connection is healthy.
- The audit log — the same record of tool calls and connections you see on your own [Audit](audit-log.md) page.

## What an operator cannot see

- **Your secrets.** Passwords, API keys, and other secret variables are write-only: once saved, they are never shown again, not on your own screen and not to an operator. They are encrypted in storage.
- **What your tools were called with.** The audit log records *which* tool ran and *whether it was allowed*, never the arguments, because arguments can themselves contain sensitive values.
- **Your identity.** An operator can't act as you or connect to the gateway as you. They only ever act as themselves.

## Operator access is recorded

When an operator steps into your organization to help, that access is logged. The reversible account actions an operator can take, ending your sessions to force a fresh sign-in or clearing a stuck connection so you can reconnect, are recorded too, along with who did them. Nothing an operator does to your organization happens silently.

## Self-hosting

If you run MCP Hero yourself, there is no outside operator. You set the operator list, so this access belongs to whoever you put on it, or to no one.
