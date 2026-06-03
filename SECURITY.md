# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public
GitHub issue for a suspected vulnerability.

Email **security@mcphero.io** with:

- a description of the issue and its impact,
- steps to reproduce (a proof of concept if you have one),
- the affected component (gateway, dashboard, sandbox runner, etc.) and
  version / commit.

We aim to acknowledge a report within a few business days and will keep
you updated on remediation. Please give us a reasonable window to ship a
fix before any public disclosure. We're happy to credit reporters who
ask for it.

## Supported versions

MCP Hero is under active development and has no long-term-support
branches yet. Security fixes land on the latest `main`; run a recent
build to stay current.

## Security model (high level)

MCP Hero is a gateway that brokers credentials and proxies
[Model Context Protocol](https://modelcontextprotocol.io) traffic
between AI clients and upstream MCP servers, for one or many
organizations. The main trust boundaries:

- **Stored credentials.** Upstream OAuth tokens and per-MCP secrets are
  encrypted at rest (cloud mode: `MCPOLIS_ENCRYPTION_KEY` over MongoDB;
  standalone: local files you are responsible for protecting — see
  "Securing secrets" in the README).
- **Untrusted MCP code.** `stdio` MCP servers run behind a
  `SandboxService` boundary. The production backend (E2B) executes them
  in isolated sandboxes; the `local-subprocess` backend runs them
  without isolation and is **dev-only** (cloud mode refuses to start
  with it).
- **Access control.** The gateway enforces a per-role access policy over
  which upstream tools each user may reach, and audits tool calls.
- **Secrets in transit/logs.** Secret-shaped values are redacted from
  server logs and (in the deployed stack) from the log-forwarding
  pipeline before they leave the host.

When deploying your own instance, review the README's cloud-mode and
"Securing secrets" sections, set strong `MCPOLIS_SESSION_SECRET` /
`MCPOLIS_ENCRYPTION_KEY` values, and terminate TLS at your reverse
proxy.
