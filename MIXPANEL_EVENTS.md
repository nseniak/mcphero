# Mixpanel events

This is the source of truth for every Mixpanel event MCP Hero emits.
When you add a new event, add it here first — the schema below is
what dashboards and funnels will rely on.

## Conventions

- **Event names** use `snake_case`, past-tense for things that happened
  (`org_created`), present-tense for ongoing actions only when the
  past-tense form is awkward.
- **Property names** use `snake_case`. Booleans start with `is_` /
  `was_` / `had_`. Counts end in `_count`. Durations end in `_ms`.
- **PII**: emails are the user identity (`distinct_id`); we never
  send tool arguments, request bodies, secrets, or display names that
  could contain PII into custom properties.
- **Source**: `backend` events are tracked server-side via
  `mixpanel-python` in a fire-and-forget background task. `frontend`
  events use `mixpanel-browser` and fire from the React app.

## Identification

| Call | When | Notes |
|---|---|---|
| `identify(email)` | After a successful sign-in (frontend), and on every backend `track(...)` (server-side `distinct_id`). | Email is the stable identity across modes. |
| `reset()` | On logout. | Clears the identified user; subsequent events go to a fresh anonymous id. |

## Super properties

Set once per session (frontend) and attached to every backend event.
Avoid duplicating these inside individual events.

| Property | Type | Source | Meaning |
|---|---|---|---|
| `distinct_id` | string | both | Authenticated user's email; anonymous Mixpanel id before login. |
| `org_id` | string | both | Slug of the user's currently active organization. Null when the user has no org (e.g. cloud signup screen). |
| `org_role` | string | both | Role of the user in the active org (e.g. `admin`, `default`). |
| `mode` | `"standalone"` \| `"cloud"` | both | Deployment mode the backend is running in. |
| `release` | string | both | Git SHA of the running build (when injected at deploy time). |
| `app_environment` | `"development"` \| `"production"` | both | Mirrors `MCPOLIS_SENTRY_ENVIRONMENT` (backend) and Vite `import.meta.env.MODE` (frontend). |

---

## Events

### Authentication & onboarding

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `landing_cta_clicked` | frontend | A visitor clicks a primary CTA on `/`, `/features`, or `/pricing`. | `cta_id` (e.g. `hero_get_started`, `pricing_team_signup`), `page` (`/`, `/features`, `/pricing`). |
| `pricing_self_hosted_notify_requested` | frontend | A visitor submits the "Notify me" form in the self-hosted "Coming soon" dialog on `/pricing`. | `notify_email` (string — captured verbatim, same rationale as `stdio_notify_requested`). |
| `signup_started` | frontend | User opens `/signup` and the form is interactive. | `mode`. |
| `signup_completed` | frontend | The signup form succeeds and the user is redirected to `/`. | `org_slug`. |
| `org_created` | backend | The `create_org` route succeeds. | `org_slug`, `created_via` (`signup` \| `manage_page` — set by the frontend; `signup` only from `/signup`, `manage_page` from the manage-orgs page), `is_first_org_for_user` (bool — measured before the new org is persisted). |
| `user_logged_in` | backend | The OAuth callback completes successfully. | `auth_method` (`google_oauth` \| `dev_header`), `was_first_user_auto_admin` (bool — true only when the auto-promote-first-user path fires in standalone mode). |
| `user_logged_out` | frontend | The user clicks "Sign out" in the dashboard header. | none |

### Organization management

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `org_switched` | frontend | The user picks a different org from `OrgSwitcher` and the page reloads. | `from_org_slug`, `to_org_slug`. |
| `org_deleted` | backend | The `delete_org` route succeeds. | `org_slug`. |

### Team management

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `user_added` | backend | An admin adds a user via the Teams page. | `target_email_hash` (sha256), `assigned_role`, `is_admin` (bool — whether the assigned role has admin privileges). |
| `user_removed` | backend | An admin removes a user. | `target_email_hash`, `removed_role`. |
| `user_role_changed` | backend | An admin changes a user's role. | `target_email_hash`, `from_role`, `to_role`. |
| `invite_link_copied` | frontend | The user clicks "Copy link" in the invite card on the Teams page. | none |

> **Why `target_email_hash`**: tracking who-added-whom at email
> granularity inside Mixpanel would mix two users' identities in a
> single event. We hash the target's email so funnels can still
> count unique invites without leaking the second person's address.

### Upstream MCPs

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `upstream_add_attempted` | frontend | The admin clicks "Add" on step 2 of the new-upstream form (regardless of outcome). | `transport` (`streamable_http` \| `stdio`), `entry_method` (`url` \| `json`). |
| `upstream_added` | backend | The `connect_upstream` route persists a new upstream definition. | `upstream_id`, `transport`, `auth_mode` (`none` \| `oauth` \| `service_account`). |
| `upstream_removed` | backend | The `disconnect_upstream` route deletes an upstream. | `upstream_id`, `transport`, `auth_mode`. |
| `upstream_oauth_completed` | backend | A user's per-user OAuth flow against an upstream finishes successfully. | `upstream_id`, `auth_mode`, `oauth_provider_domain` (e.g. `accounts.google.com` — netloc of `upstream.http.url`). |
| `upstream_oauth_failed` | backend | The same flow fails. | `upstream_id`, `auth_mode`, `failure_reason` (enum: `user_denied` — the user cancelled in the popup; `token_exchange` — the OAuth flow ran but no tokens were stored; `discovery` — the upstream was unreachable / timed out before authorization started; `unknown` — fallback for unclassified errors, e.g. post-refresh connection failure). |
| `upstream_connect_clicked` | frontend | A user clicks "Connect" on the `/connect` page for an upstream. | `upstream_id`. |
| `stdio_mcp_attempted` | frontend | The admin clicks "Next" on the new-upstream form with JSON that defines a stdio MCP (i.e. has a `command` field). | `was_blocked` (bool — true when `allow_stdio_mcp=false`; the user sees a promotional dialog instead of advancing). |
| `stdio_notify_requested` | frontend | The admin clicks "Notify me" inside the stdio promo dialog, opting in to be emailed when stdio MCPs ship. | `notify_email` (string — the signed-in user's email, captured verbatim so we keep the waitlist even if `distinct_id` identity resolution changes later). |

> **`stdio_mcp_attempted`**: this exists to size the demand for stdio
> support in cloud mode. `was_blocked=true` events tell us how often
> the policy bites; `was_blocked=false` events tell us how many
> standalone users actually rely on stdio. Don't fold this into
> `upstream_add_attempted` — keeping it separate makes the demand
> dashboard a single-event filter.

### Permissions

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `role_mcp_access_changed` | backend | The `set_role_mcp_access_entry` route succeeds. | `role_name`, `upstream_id`, `enabled` (bool — whether the role can use the upstream at all). |
| `role_tool_access_changed` | backend | The `set_role_tool_access_entry` route succeeds. | `role_name`, `upstream_id`, `tool_name`, `decision` (`allow` \| `deny`). |

### Tool usage

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `tool_called` | backend | Inside `tool_router.route_call`, hooked into the existing audit-log `finally` block so audit and analytics never disagree. | `upstream_id`, `tool_name` (the prefixed name as exposed to clients), `auth_mode`, `response_status` (`success` \| `error`), `latency_ms` (int), `had_session_id` (bool — whether the call carried a gateway session). |

> Tool **arguments** are deliberately excluded — they routinely
> contain free-form user input. Argument *count* and presence of
> specific known-safe keys could be added later if a need emerges.

### Navigation & engagement (frontend)

| Event | Source | When it fires | Custom properties |
|---|---|---|---|
| `page_viewed` | frontend | A route change inside the SPA (mounted via a `useAnalyticsPageView` hook in both `MarketingLayout` and `DashboardLayout`). | `path`, `is_authenticated` (bool), `surface` (`marketing` \| `app`). |

---

## What we deliberately don't track

These were considered and excluded:

- **Tool arguments / response bodies** — PII risk.
- **OAuth tokens, secrets, cookie contents** — never.
- **Per-keystroke form events** — too noisy; `*_completed` events
  cover funnel needs.
- **Health / startup pings** — Sentry traces cover backend
  performance; Mixpanel is for product behavior.
