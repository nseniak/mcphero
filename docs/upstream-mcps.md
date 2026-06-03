# Upstream MCPs

Everything you do on the **Upstream MCPs** page: adding MCPs, connecting them, refreshing their tool list, editing them, and removing them.

If "upstream MCP", "HTTP MCP", or "stdio MCP" are unfamiliar, skim [Concepts](concepts.md) first.

## Adding an HTTP MCP

For hosted, remote MCPs reached at a URL — Mixpanel, HubSpot, Stripe, your own internal HTTP MCP.

1. Open **Upstream MCPs** and click **Add MCP**.
2. **Step 1 — Configuration.** On the **URL** tab, paste the upstream's URL. (Or use the **JSON** tab if you have an `mcpServers` block from elsewhere.) Click **Next**.
3. **Step 2 — Details.** MCP Hero pre-fills:
   - **ID** — a short identifier derived from the hostname (`mcp.slack.com` → `slack`). This becomes part of every tool name (`slack__send_message`). Edit if you want.
   - **Display Name** — derived from the ID. Shown in the dashboard and to your team.
4. Pick the **User authentication mode** (see *User authentication modes* in [Concepts](concepts.md)):
   - **None** — for an open MCP, or one whose credentials live in the request itself (e.g. a shared API key in a `headers` block of the JSON config).
   - **Shared** — you sign in once with OAuth on the team's behalf.
   - **Per-user** — every team member signs in personally. *(Default for HTTP MCPs.)*
5. (Optional) Open **OAuth client credentials** to provide a **Client ID** and **Client Secret**. Only needed for OAuth servers that don't support dynamic client registration — GitHub is the most common example.
6. Click **Add**. The MCP appears in the list.

> **Trailing slash matters**
>
> Many MCP servers built on FastMCP or Starlette respond with a `307 Temporary Redirect` from `/mcp` to `/mcp/`, which breaks the Streamable HTTP session handshake. If a connection fails with `Session terminated` or `unhandled errors in a TaskGroup`, paste the URL with a trailing slash (`https://example.com/mcp/`) and re-add the MCP. This is a known issue in the wider MCP ecosystem, not specific to MCP Hero.

## Adding a stdio MCP

For MCPs distributed as a CLI command (typically `npx @scope/server-name`). MCP Hero runs the command for you in a hosted sandbox; nothing installs on anyone's laptop.

1. Click **Add MCP**, switch to the **JSON** tab.
2. Paste a config block, either direct:

   ```json
   {
     "command": "npx",
     "args": [
       "-y",
       "@modelcontextprotocol/server-postgres",
       "${DATABASE_URL}"
     ]
   }
   ```

   or wrapped (the standard `mcpServers` format other tools also accept):

   ```json
   {
     "mcpServers": {
       "warehouse": {
         "command": "npx",
         "args": [
           "-y",
           "@modelcontextprotocol/server-postgres",
           "${DATABASE_URL}"
         ]
       }
     }
   }
   ```

3. Fill in **Variables** for any `${NAME}` reference in the JSON (see *Variables* below).
4. Click **Next**. On the **Details** step, MCP Hero pre-fills the **ID** and **Display Name** from the package name (`@modelcontextprotocol/server-postgres` → `postgres`).
5. **User authentication mode** is fixed to **None** for stdio MCPs (browser-based OAuth doesn't work in a remote sandbox; see [Stdio MCP authentication](stdio-authent.md) for the full reasoning). The picker is hidden — credentials go in Variables and Files instead.
6. Pick **Sandbox** → **Resources**. The dropdown lists CPU / memory / disk combinations; the default is the smallest combo. Bump it for memory-hungry servers.
7. Click **Add**.

> **Why the JSON tab for stdio MCPs?**
>
> The **URL** tab assumes HTTP. Stdio MCPs are commands, not URLs, so they have to come in via JSON.

### Variables

Variables are a per-MCP key/value store. Anywhere in the JSON config — `command`, `args`, `url`, `env` values, `headers` values — write `${NAME}` and the gateway substitutes the Variable's value when it launches the MCP. The literal `\${NAME}` (with the backslash) escapes substitution.

Names must be uppercase letters, digits, and underscores, starting with a letter or underscore (`DATABASE_URL`, `GH_TOKEN`, `NOTION_API_KEY`).

The Variables panel sits right below the JSON editor on Step 1.

- Every `${NAME}` you've referenced but not yet defined shows up in an amber **undefined variables** callout with an `[+ Add]` button. Click it to define the Variable inline.
- Each Variable has a **Treat as password** toggle (default on). Passwords render as `••••XYZ4` in the list with an eye icon to reveal, the way 1Password does it. Plain Variables render their value verbatim.
- Password values are encrypted at rest in cloud mode and redacted from the server-logs panel at write time. Plain values are not.
- A Variable's name is editable in **Replace** mode; the rename happens atomically on save.

One built-in Variable is always available: `${HOME}` resolves to the sandbox user's home directory (`/home/user` on every shipped template). System Variables show up in the list with a "system" badge and can't be edited or deleted.

Variables can't reference other Variables — substitution is a single pass against the (system + user) pool.

Don't want to type `${NAME}` placeholders by hand? Paste the credential straight into the JSON. If MCP Hero recognises the shape (API token, bearer header, …), it pops up a dialog offering to extract the value into a Variable for you (see *Inline secret detection* below).

### Files

For SDKs that read credentials from a file path on disk — Google service-account JSON, kubeconfig, AWS shared credentials, TLS client certs, custom YAML — Variables aren't enough. Use **Files**.

Files are configured on the upstream's detail page after you create it: open the MCP, click **Edit**, and use the **Files** section under **Configuration**.

For each file you set:

- **Display name** — free-form label shown in the listing (defaults to the uploaded filename).
- **Target path** — where the file lands inside the sandbox at session start. Accepts the same `${...}` references as the JSON config (system + user Variables), so a per-tenant path like `${HOME}/.config/myapp/${TENANT_ID}/creds.json` works.
- **Contents** — the bytes you upload (drag-drop or pick). Capped at 128 KiB; an accidental binary upload errors immediately.

The launcher writes each file with mode `0600` and creates parent directories as needed. Contents are encrypted at rest in cloud mode and never re-rendered in the dashboard (you see size + truncated sha256, not the bytes) so a credential file doesn't leak by accident.

The canonical recipe for "SDK wants a path, value is in the file" is to pair a File with a Variable that points at the same path:

**File:**

- **Target path:** `${HOME}/.config/gcloud/credentials.json`
- **Contents:** the service-account JSON, uploaded via drag-drop or the file picker.

**Variable:**

- **Name:** `GOOGLE_APPLICATION_CREDENTIALS`
- **Value:** `${HOME}/.config/gcloud/credentials.json`

At launch, both render to `/home/user/.config/gcloud/credentials.json`. The Variable is then used wherever the SDK reads it (typically `env` in the JSON config). The same recipe works verbatim for `KUBECONFIG`, `AWS_SHARED_CREDENTIALS_FILE`, and any `--credentials=PATH` flag.

For SDKs that read from a fixed well-known path (kubeconfig at `~/.kube/config`, AWS at `~/.aws/credentials`), the Variable step is unnecessary — just set the file's `target_path` to the well-known location and the SDK finds it on its own.

More on what's supported and what isn't: [Stdio MCP authentication](stdio-authent.md).

## Inline secret detection

When you paste JSON that contains a string that looks like an API token or a bearer header, MCP Hero pops up a dialog offering to extract it into a Variable for you. Approve, name the Variable, and the JSON is rewritten with `${NAME}` while the value lands (encrypted at rest in cloud mode) in the Variables list. Decline if it's a false positive.

The dialog also fires on **Save** when you edit an existing MCP's JSON. You can suppress it per-MCP via "Don't show again"; the **Show password detection again** link in the Variables card brings it back.

## Importing several MCPs at once

The **Import JSON config** button next to **Add MCP** accepts a multi-server `mcpServers: {...}` JSON file (the standard format Claude Desktop, Cursor, and others write). It previews each entry, lets you pick which to import, and creates them in one go.

## Connecting an MCP

Newly added MCPs appear with **Status: Disconnected**. The action button on the row depends on the upstream's transport and auth mode:

- **HTTP, None** — **Connect**. MCP Hero opens the session and discovers tools.
- **HTTP, Shared / Per-user** — **Authenticate**. MCP Hero opens the upstream's OAuth consent screen in a new tab. After consent, tool discovery happens. (For Per-user, the *first* sign-in does double duty as tool discovery.)
- **Stdio, None** — **Start**. MCP Hero spins up the sandbox, runs the command, and discovers tools. Cold starts can take 30–60s for `npx`/`uvx` package pulls; the row shows **Starting…** with a spinner.

If a connection fails, the row shows **Couldn't connect** and a one-line error. The most common causes:

- Wrong URL (missing trailing slash, wrong path)
- Wrong auth mode for the upstream
- The upstream doesn't support dynamic client registration → set Client ID/Secret under **OAuth client credentials**
- An undefined `${NAME}` reference → fill the Variable in
- A stdio MCP that opens a browser to authenticate → not supported in the sandbox; see [Stdio MCP authentication](stdio-authent.md)

## Disconnecting / stopping an MCP

When the MCP is Connected, the action button becomes **Disconnect** (HTTP) or **Stop** (stdio). Use it to drop the live session without removing the MCP. The tool list, Variables, and Files are preserved; the connection is closed. (For OAuth modes, disconnecting also clears the stored tokens.)

## Refreshing an MCP's tools

MCP Hero re-discovers an upstream's tool list automatically when the upstream notifies it of changes — you don't need to do anything. The post-connect refresh runs in the background; the row shows a small **Fetching info…** spinner until the new tool count is in.

## Editing an MCP

Click an MCP's display name to open its detail page. The page shows three cards:

- **Overview** — ID, Display Name, Transport, URL/Command, User authentication mode (HTTP only).
- **Configuration** — the JSON config, Variables, Files (stdio only), and OAuth client credentials (OAuth modes only).
- **Sandbox** — Resources picker and Execution logs (stdio only).

Click **Edit** in the toolbar above the cards to switch the page into edit mode. You can change every field inline — Display Name, auth mode (HTTP), the JSON config (URL/headers/command/args/env), Variables, Files, OAuth client credentials, and Sandbox resources. Click **Save** to commit; **Cancel** to revert.

Saving a configuration change **does not auto-disconnect** the running session. A **Configuration changed** banner appears at the top of the page; the new config takes effect on the next reconnect (HTTP) or start (stdio).

## Execution logs (stdio only)

The **Sandbox** card has an **Execution logs** disclosure that streams the wrapped MCP's stderr in real time. Useful for debugging command failures, missing dependencies, or auth errors during init. Logs reset on every Start / Reconnect click.

## Removing an MCP

In the list, click the trash icon at the end of the row, then confirm. From the detail page, click **Edit** to enter edit mode — a **Remove** button appears at the bottom of the edit area.

Removing an MCP:

- Disconnects (or stops) it
- Deletes its config (URL/command/args/env)
- Deletes its Variables and Files
- Removes its tools from your team's gateway view
- Drops any per-tool overrides or argument checks attached to it (in roles)

This is irreversible — re-add the MCP if you change your mind.
