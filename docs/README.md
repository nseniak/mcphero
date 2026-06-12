# MCP Hero admin docs

Admin documentation for setting up and running an MCP Hero organization.

These docs are written for the person who creates the organization and configures its MCPs, roles, and audit. End users (your team members who connect their AI client to the gateway and call tools) shouldn't need to read these.

If you're brand new, start with **Getting started**. If you know what you're looking for, jump in.

## Contents

- **[Concepts](concepts.md)** — terms used throughout the docs, with callouts for HTTP MCPs vs stdio MCPs and the three user authentication modes.
- **[Getting started](getting-started.md)** — walkthrough that takes you from sign-up to your first MCP shared with your team.
- **[Upstream MCPs](upstream-mcps.md)** — adding, connecting, editing, and removing the MCP servers that MCP Hero proxies.
- **[Stdio MCP authentication](stdio-authent.md)** — what credential patterns work for stdio MCPs (Variables, Files), what doesn't (browser OAuth), and why.
- **[Running your own MCP code](your-own-mcp-code.md)** — distributing an MCP server you wrote yourself, as a public package or as a private package with a registry credential, plus when to host it as an HTTP MCP instead.
- **[Team](team.md)** — inviting members, changing roles, removing members.
- **[Roles and permissions](roles-and-permissions.md)** — the access model: roles, per-MCP and per-tool access, argument checks.
- **[Service tokens](service-tokens.md)** — connecting headless agents (CI jobs, bots, server-side AI agents) to the gateway with a revocable bearer token bound to a role.
- **[Audit log](audit-log.md)** — what gets logged, and how to search it.
- **[Admin MCP](admin-mcp.md)** — managing MCP Hero conversationally from your AI assistant.
