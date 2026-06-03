# Stdio MCP authentication

How to give a stdio MCP the credentials it needs, what credential patterns MCP Hero supports, and what doesn't work (and why).

If you're new to upstream MCPs, [Concepts](concepts.md) and [Upstream MCPs](upstream-mcps.md) are the better entry points.

## Why stdio MCPs are different

When you add an HTTP MCP, your team's AI client signs in to that MCP via OAuth. MCP Hero handles the consent flow and stores the tokens.

Stdio MCPs aren't web servers — they're commands. MCP Hero runs the command for you in a hosted sandbox and talks to it over its standard input/output. There's no OAuth conversation between MCP Hero and a stdio MCP, because there's nothing for OAuth to flow over.

So stdio authentication is a different question: **how does the wrapper (the running command) get the credential it needs to talk to its upstream service** (GitHub, Postgres, Slack, your data warehouse, etc.)?

Most stdio wrappers expect the credential as an environment variable or a file on disk. MCP Hero supports both via **Variables** and **Files** on the upstream's detail page (see [Upstream MCPs](upstream-mcps.md)).

## What works

### No auth

Wrappers that operate on local resources or open data — the filesystem MCP, the time MCP, the memory MCP, sqlite pointed at a writable file. Add the MCP and click **Start**. No credentials to configure.

### Tokens, API keys, connection strings (use Variables)

Most stdio wrappers expect a credential in an environment variable. Examples:

| MCP | Variable name |
| --- | --- |
| GitHub | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| Slack | `SLACK_BOT_TOKEN` |
| Notion | `NOTION_API_KEY` |
| Linear | `LINEAR_API_KEY` |
| Sentry | `SENTRY_AUTH_TOKEN` |
| Postgres | `DATABASE_URL` (connection string) |

Reference these in the JSON config with `${NAME}` and define them in the **Variables** panel:

```json
{
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
  }
}
```

Toggle the Variable's **Treat as password** to hide it in the dashboard and redact it from logs.

### Credentials that need a file on disk (use Files)

A handful of upstream SDKs read credentials from a file path, not an env value. The classic cases:

- **`GOOGLE_APPLICATION_CREDENTIALS`** — Google's official client libraries accept only a path to a service-account JSON file. There's no inline-JSON variant in the standard SDKs.
- **`KUBECONFIG`** — points at a kubeconfig YAML.
- **`AWS_SHARED_CREDENTIALS_FILE`** / **`AWS_CONFIG_FILE`** — point at the AWS shared-credentials INI files.
- **TLS client certs** — `--cert /path/to/client.pem` flags or config keys.
- **Custom config files** — bespoke per-server YAML / JSON.

For these, upload the file in the **Files** section of the upstream's detail page and pick where it should land in the sandbox.

#### Worked recipe: GCP service account

This is the canonical "wrapper wants a path, value lives in the file" pattern. You configure two things, both pointing at the same path:

In the **Files** section, upload your service-account JSON and set:

- **Target path:** `${HOME}/.config/gcloud/credentials.json`

In the **Variables** section, add:

- **Name:** `GOOGLE_APPLICATION_CREDENTIALS`
- **Value:** `${HOME}/.config/gcloud/credentials.json`

In the JSON config, wire the Variable into the wrapper's environment:

```json
{
  "command": "...",
  "args": ["..."],
  "env": {
    "GOOGLE_APPLICATION_CREDENTIALS": "${GOOGLE_APPLICATION_CREDENTIALS}"
  }
}
```

When the MCP starts:

1. `${HOME}` resolves to the sandbox user's home directory.
2. Your uploaded file lands at `/home/user/.config/gcloud/credentials.json`.
3. The wrapper sees `GOOGLE_APPLICATION_CREDENTIALS` set to that absolute path and reads the file.

The same recipe works for `KUBECONFIG`, `AWS_SHARED_CREDENTIALS_FILE`, and any `--credentials=PATH` flag.

For SDKs that read from a fixed well-known path (kubeconfig at `~/.kube/config`, AWS at `~/.aws/credentials`), the Variable step is unnecessary — just set the file's target path to the well-known location and the SDK finds it.

For per-tenant or per-profile paths, use a Variable inside the target path, e.g. `${HOME}/.config/myapp/${TENANT_ID}/creds.json`.

## What doesn't work: browser-based OAuth

Some stdio MCPs ship with their own embedded OAuth flow: the wrapper opens a browser window pointing at the OAuth provider, runs a local callback listener, and waits for the user to consent. The archived `@modelcontextprotocol/server-gdrive` is the canonical example.

This pattern doesn't work in a hosted sandbox, for three independent reasons:

1. **No browser to open.** The sandbox has no display server. Telling it to "open a browser" does nothing the user can see.
2. **No way to reach the local callback.** The wrapper expects the user's browser to redirect to `localhost:<port>` — but that "localhost" is inside the sandbox's network. The user's browser can't reach it.
3. **Nothing to deliver the result.** Even if the first two were fixed, OAuth providers refuse to redirect to arbitrary public URLs for installed-app flows.

So if you add an MCP that uses this pattern, the connection hangs at startup until MCP Hero gives up. The dashboard shows a clear error explaining what to try instead — typically:

- Use the streamable-HTTP version of the MCP if one exists (most popular wrappers offer both).
- Use a community fork that accepts a token via env var or a credential file.

We do not currently support a "gateway-bridged" workaround that brokers the OAuth flow on the dashboard side and injects the token into the wrapper. It's on the radar; if a specific MCP would unblock you, get in touch.
