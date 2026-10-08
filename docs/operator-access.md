# Operator access

MCP Hero stores credentials for the MCP servers your team connects, so it's fair to ask: what can the people who run MCP Hero actually see? This page answers that.

## The operator role

MCP Hero has an **operator** role (you may see it called `superadmin` in our open-source code). It's how the people running the hosted service keep it healthy: spotting an organization whose connections are failing, helping a member who's locked out, or removing an organization on request.

The operator role is an explicit list of email addresses, set when the service starts. On a self-hosted MCP Hero, that list is yours to control, so the only people with this access are the ones you name, or nobody if you leave it empty.

## What an operator can see

- Organization names, member lists, and which MCP servers you've connected, along with whether each connection is healthy.
- The audit log — the same record of tool calls and connections you see on your own [Audit](audit-log.md) page.

## What an operator cannot see

- **Your passwords.** Variables marked as passwords are write-only: once saved, the dashboard and the admin API never show them again, to you or to an operator. They are encrypted in storage. Two limits: a value typed straight into an MCP's JSON settings, rather than into a password Variable, stays visible to your admins and to an operator helping as one; and anyone with admin rights can change where an MCP sends its credentials.
- **What your tools were called with.** The audit log records *which* tool ran and *whether it was allowed*, never the arguments, because arguments can themselves contain sensitive values.
- **Your identity.** An operator can't act as you or connect to the gateway as you. They only ever act as themselves.
- **Your MCP servers' accounts.** An operator can't sign in to the MCP servers your team connects: only your members' sign-ins are kept.

## Operator access is recorded

When an operator steps into your organization to help, that access is logged on our side. These operator actions also appear on your own [Audit](audit-log.md) page, tagged **MCP Hero operator** and naming who did them: ending a member's sessions to force a fresh sign-in, clearing a stuck connection so they can reconnect, changing your plan, and removing a teammate. Other changes an operator makes while helping, such as editing a role, are in our own logs but not yet on your Audit page.

## Self-hosting

If you run MCP Hero yourself, there is no outside operator. You set the operator list, so this access belongs to whoever you put on it, or to no one.
