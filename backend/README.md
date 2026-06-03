# Backend (MCP Hero)

Python service that exposes a single MCP endpoint and proxies to multiple upstream MCP servers with:

- Role-based access control (per-user, per-MCP, per-tool)
- OAuth gateway authentication (Google) + upstream OAuth brokering
- Admin dashboard for managing MCPs, users, roles, and access policies
- Audit logging
- Credential store (file-based)

## Running

MCP Hero is run from the repo root, not from `backend/`. See the root
[README](../README.md) for getting started — this avoids duplicating
(and drifting from) the canonical instructions:

- **[Quick start](../README.md#quick-start-standalone)** — try it with Docker, no toolchain required.
- **[Develop from source](../README.md#develop-from-source)** — `bash start.sh [standalone|cloud]` with hot reload.
- **[The switches that decide how it runs](../README.md#the-three-switches-that-decide-how-it-runs)** — `MCPOLIS_MODE` / `MCPOLIS_OAUTH_PROVIDER` / `MCPOLIS_SANDBOX_PROVIDER`.

The rest of this file is the backend's own configuration reference.

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCPOLIS_SERVER_URL` | `http://localhost:8000` | Public URL of the MCP Hero instance |
| `MCPOLIS_OAUTH_PROVIDER` | `dev_stub` | `google` for real Google sign-in, or `dev_stub` for the in-app dev email picker (dev only; rejected in cloud mode) |
| `MCPOLIS_GOOGLE_CLIENT_ID` | | Google OAuth client ID (required when `MCPOLIS_OAUTH_PROVIDER=google`) |
| `MCPOLIS_GOOGLE_CLIENT_SECRET` | | Google OAuth client secret |
| `MCPOLIS_MCP_JSON_PATH` | `config/mcp.json` | Path to MCP server definitions |
| `MCPOLIS_CONFIG_PATH` | `config/config.json` | Path to MCP Hero settings (roles, users, upstream options) |
| `MCPOLIS_OAUTH_APPS_PATH` | `config/oauth_apps.json` | Path to instance-level OAuth app credentials |
| `MCPOLIS_DATA_DIR` | `data` | Directory for persistent data (tokens, connection state) |
| `MCPOLIS_SESSION_SECRET` | | Dashboard session signing key |

### Config Files

- **`config/mcp.json`** — Standard `mcpServers` format defining upstream MCP connections (URLs, commands, headers). Editable from the admin UI.
- **`config/config.json`** — MCP Hero settings: roles, users, per-upstream options (display name, auth mode, scopes, OAuth credentials). Managed by the admin UI.
- **`config/oauth_apps.json`** — Instance-level OAuth app credentials keyed by domain. Config-file-only (not editable from the UI). See [Connecting GitHub's MCP Server](#connecting-githubs-mcp-server) for details.

### Authentication

Auth is selected by `MCPOLIS_OAUTH_PROVIDER`:

- **`google`** — real Google sign-in. MCP Hero matches the user's email to roles and enforces access policies. Required in **cloud** mode. See [Google OAuth Setup](#google-oauth-setup).
- **`dev_stub`** (default) — an in-app email picker that issues a real signed session without contacting Google. **Dev only**; it's rejected at startup in cloud mode. `bash start.sh --fake-auth` selects it (plus a gateway test-token endpoint) for local development.

Role and user management is done through the admin dashboard.

> Standalone vs cloud mode (file-backed vs MongoDB/Redis, single- vs multi-org) is described in the [root README](../README.md).

## Google OAuth Setup

Required when `MCPOLIS_OAUTH_PROVIDER=google` (real sign-in, and always in cloud mode).

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Create an **OAuth client ID** (Web application)
3. Set **Authorized JavaScript origins** to your MCP Hero URL
4. Set **Authorized redirect URIs** to `https://your-domain.com/mcp/oauth/google/callback`
5. Copy the Client ID and Client Secret into your `.env`:

```env
MCPOLIS_OAUTH_PROVIDER=google
MCPOLIS_GOOGLE_CLIENT_ID=<your client id>
MCPOLIS_GOOGLE_CLIENT_SECRET=<your client secret>
```

## Connecting GitHub's MCP Server

GitHub's remote MCP server (`https://api.githubcopilot.com/mcp/`) supports two authentication methods. Choose the one that fits your needs.

### Option A: Personal Access Token (simplest)

Use a [GitHub Personal Access Token](https://github.com/settings/tokens) for quick setup. In the MCP Hero admin UI:

1. Click **Add MCP**
2. Select the **JSON** tab and enter:
   ```json
   {
     "url": "https://api.githubcopilot.com/mcp/",
     "headers": {
       "Authorization": "Bearer ghp_your_token_here"
     }
   }
   ```
3. Authentication will be set to **Service Account** automatically
4. Click **Add** — the MCP connects immediately

### Option B: OAuth App (recommended for teams)

OAuth lets each user authorize with their own GitHub account. GitHub does not support dynamic client registration, so you need to register an OAuth App first.

#### 1. Create a GitHub OAuth App

1. Go to [GitHub Developer Settings → OAuth Apps → New OAuth App](https://github.com/settings/developers)
2. Fill in the form:
   - **Application name**: e.g. `MCP Hero`
   - **Homepage URL**: your MCP Hero URL (e.g. `https://your-domain.com`)
   - **Authorization callback URL**: `https://your-domain.com/api/oauth/upstream/callback`
     (replace `your-domain.com` with your actual domain or ngrok URL)
3. Click **Register application**
4. Copy the **Client ID** and generate a **Client Secret**

#### 2. Configure the credentials

**Per-upstream (via admin UI):** Add or edit the GitHub MCP, expand **Advanced settings**, and enter the OAuth Client ID and Client Secret.

**Instance-level (via config file):** To apply credentials to all upstreams on the same domain, create `config/oauth_apps.json`:

```json
{
  "githubcopilot.com": {
    "client_id": "your-client-id",
    "client_secret": "your-client-secret"
  }
}
```

When an upstream's URL hostname matches a configured domain (e.g. `api.githubcopilot.com` matches `githubcopilot.com`), the credentials are used automatically. Per-upstream credentials in the admin UI take priority as an override. The file is read at startup — restart MCP Hero after editing it.

#### 3. Authenticate

Click **Authenticate** on the GitHub MCP. A popup will open to GitHub's OAuth consent screen. After authorizing, the connection will be established and GitHub's tools will become available.

### Why are OAuth credentials needed?

MCP hosts like Claude Desktop and VS Code ship with their own pre-registered GitHub OAuth Apps, so OAuth works out of the box. Third-party MCP gateways like MCP Hero need to register their own. This is a [requirement from GitHub](https://github.com/github/github-mcp-server): *"Each MCP host application needs to configure a GitHub App or OAuth App to support remote access via OAuth."*

This same pattern applies to any MCP server that doesn't support dynamic client registration.

