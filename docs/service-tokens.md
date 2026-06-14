# Service tokens

How to connect a headless agent — a CI job, a scheduled script, an AI agent running on a server — to your organization's gateway without an interactive sign-in.

If you're new to the gateway, start with [Getting started](getting-started.md). For the human side of connecting (Google sign-in from an AI client), see the **Gateway MCP** page in your dashboard.

## What a service token is

Your team members connect to the gateway by signing in with Google from their AI client. That works for people, but not for software: a bot running in a container has no browser to complete the sign-in.

A **service token** is a bearer credential that takes the place of the sign-in. You create it in the dashboard, choose which **role** it acts as, and paste it into your agent's configuration. From then on the agent connects like any other client — except its access comes from the role you picked, and its activity is logged under the token's name.

Key properties:

- **One organization, one role.** A token is bound to the organization it was created in and to the role you chose. It can't reach any other organization, and its tool access is exactly what that role allows.
- **Shown once.** The token value appears a single time, when you create it. It can't be recovered later — not by you, not by us. Store it in your secret manager.
- **No expiry, revocable anytime.** Tokens stay valid until you revoke them. Revoking takes effect on the agent's next request.
- **Not a seat.** Service tokens don't appear on the Team page and don't count toward your plan's member seats.

## Creating a token

1. Open **Service Tokens** in the sidebar (admins only).
2. Click **New token**.
3. Pick a **name** (lowercase letters, digits, `-`, `_`). The name becomes the token's identity everywhere — choose something that says what the agent is, like `ci-bot` or `support-agent`.
4. Pick a **role**. Create a dedicated least-privilege role first on the **Roles & Permissions** page if none fits — give the agent only the MCPs and tools it actually needs.
5. Click **Add**, then **copy the token value immediately**. This is the only time it is shown.

The panel also shows a ready-to-paste MCP JSON snippet with the token filled in.

## Configuring your agent

Point the agent at your organization's gateway URL (shown on the **Gateway MCP** page) and send the token as a bearer credential:

```json
{
  "mcpServers": {
    "mcphero": {
      "url": "https://mcphero.io/mcp/your-org",
      "headers": {
        "Authorization": "Bearer svct_..."
      }
    }
  }
}
```

Any MCP client that supports custom headers works the same way — the gateway accepts the token wherever it would accept a signed-in user's credential. A quick connectivity check with `curl`:

```bash
curl -s https://mcphero.io/mcp/your-org/ \
  -H "Authorization: Bearer svct_..." \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

Use the organization-scoped URL (`/mcp/your-org`) from the dashboard. The token only works for its own organization — pointing it at another organization's URL is rejected.

> **Gateway URL by mode.** The examples above are cloud (`mcphero.io`), where the gateway is org-scoped: `/mcp/<org-slug>`. In **standalone** mode there's a single `default` org and the gateway is served at the bare `/mcp` (e.g. `http://localhost:8080/mcp`) — no slug. Whichever mode you run, copy the exact URL shown on the dashboard's **Gateway MCP** / **Connect AI Assistant** page rather than hand-building it.

## Tokens in the audit log

Every call an agent makes appears in the [audit log](audit-log.md) under the identity `svc:<name>` — for example `svc:ci-bot`. You can filter the **User** column by that identity just like a member email. This is why each agent should get its own token: separate tokens mean separate audit trails and independent revocation.

## Rotating and revoking

- **Revoke** — click the trash icon next to the token. The agent's next request fails with an authentication error. Revoke immediately if a token may have leaked.
- **Rotate** — create a new token (a temporary second name like `ci-bot-2` is fine), switch your agent's configuration to it, then revoke the old one.

The **Last used** column shows when each token last made a request — useful for spotting stale tokens that can be cleaned up.

## Handling the secret

A service token is a credential with real access to your tools. Treat it like a password:

- Store it in a secret manager (Kubernetes Secrets, GitHub Actions secrets, 1Password, etc.) — never in source control.
- Give each agent its own token rather than sharing one.
- Prefer a narrow, dedicated role over reusing a broad one.
- Revoke tokens you no longer use.
