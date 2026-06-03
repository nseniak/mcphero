# Audit log

Every tool call and every gateway connection event in your organization is logged. The **Audit** page is where you search them.

## What gets logged

- **Tool call** — every tool call: who called what, with which arguments, what came back, how long it took, and whether the call was allowed or denied by your role policies.
- **Connection** — when a team member's AI client connects or disconnects from the gateway.

Denied tool calls are logged too — the policy check happens before the call is forwarded, and the deny reason is recorded.

## Searching the log

The page shows the most recent entries first, capped at 100 per view. Filter using the controls at the top of the page:

- **User** — autocompleted from emails seen in the log. Shows only entries triggered by that member.
- **MCP** — autocompleted from upstream MCP IDs. Shows only entries against that upstream.
- **Tool** — free-text. Substring match against the full tool name (e.g. typing `query` matches `mixpanel__query_events` and `database__run_query`).
- **Action** — dropdown:
  - **All actions** (default)
  - **Tool call** — only tool calls
  - **Connection** — connect and disconnect events together

Filters combine — they all have to match for an entry to appear.

The columns are: **Time**, **Action**, **User**, **MCP**, **Tool / Details**, **Outcome**, **Latency**. The Tool/Details column shows the full tool name for tool calls, or the AI client type (Claude, ChatGPT, etc.) for connection events.

## Live updates

The play/pause button at the top-left of the filter bar controls **live mode**:

- **Live** (green, default) — new entries stream in as they happen via Server-Sent Events. Filters apply to incoming entries the same way they apply to existing ones.
- **Paused** — the view freezes. Useful for taking a snapshot, scrolling through what's there, or copy/pasting an entry without it scrolling away.

Clicking pause doesn't stop logging — it just stops the dashboard from showing new entries until you press play again.
