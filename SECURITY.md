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

## The operator (superadmin) role

Multi-tenant cloud deployments have an **operator** role — `superadmin`
in the code — gated by the `MCPOLIS_SUPERADMIN_EMAILS` allowlist (a
comma-separated env var read once at startup). It's empty in
single-org / standalone deployments. Every SaaS operator has privileged
access to the system they run; this one is explicit and bounded so it
can be audited here rather than taken on trust. On the hosted service
the allowlist is the operator alone; if you self-host, you control it
(set it to yourself, or leave it empty).

**The operator role can:** read cross-organization *metadata* (org
names, member rosters, plans, upstream names / transport / connection
status, OAuth token *health* — never token values); search the audit
log across orgs; change an org's plan; take soft, reversible account
actions (revoke a user's gateway sessions, clear a stuck OAuth
connection); delete an org behind an explicit confirmation; and enter an
org's admin surface to support it. The mutations and the org-entry are
recorded with the operator's identity.

**The operator role cannot:** impersonate a user or forge gateway
credentials (it acts only as itself); read back your secrets (secret
variables and upstream credentials are *write-only* — accepted when set,
never returned by any read API, even to an operator inside your org); or
see tool-call arguments (the audit log records *what* ran and *whether
it was allowed*, not argument values).

When an operator enters an organization they don't belong to, an access
record carrying the operator's identity and the target org is written to
the log pipeline — so every operator touch of a customer org leaves a
trail.
