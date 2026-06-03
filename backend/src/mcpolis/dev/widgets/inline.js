// Inline widget body — loaded fresh from
// PUBLIC_URL/dev/mcp-demo/widget/inline.js on each iframe render.
// Edits here do NOT require removing and re-adding the connector:
// the MCP resource URI is a stable shell that dynamically imports
// this file with a cache-busting query string.

import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@^1/app-with-deps";

// Build DOM programmatically (no innerHTML) so we stay XSS-hook-friendly.
function el(tag, props = {}, kids = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") e.textContent = String(v);
    else if (k === "css") e.setAttribute("style", v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, String(v));
  }
  for (const k of kids) e.appendChild(k);
  return e;
}

export default async function mount(root) {
  const style = document.createElement("style");
  style.textContent = `
    html, body { margin: 0; padding: 0; }
    body {
      width: 480px; height: 260px;
      box-sizing: border-box; padding: 28px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #fef3c7; border: 8px solid #d97706; color: #111;
      display: flex; flex-direction: column; gap: 14px; justify-content: space-between;
    }
    h1 { font-size: 24px; margin: 0; color: #92400e; }
    p  { font-size: 14px; margin: 0; line-height: 1.4; }
    button {
      align-self: flex-start; padding: 12px 18px; font-size: 15px;
      border: 0; border-radius: 10px; background: #111; color: #fff; cursor: pointer;
    }
    button:hover { background: #333; }
    button:disabled { background: #999; cursor: not-allowed; }
    #status { font-size: 11px; color: #666; }
  `;
  document.head.appendChild(style);

  const statusEl = el("div", { id: "status", text: "connecting…" });
  const askBtn = el("button", { id: "ask", disabled: true, text: "Tell me a joke" });
  root.append(
    el("h1", { text: "MCP Hero demo — inline" }),
    el("p", { text: "Click the button to send a follow-up prompt back into the chat." }),
    askBtn,
    statusEl,
  );

  const app = new App({ name: "mcphero-inline-demo", version: "1.0.0" });
  try {
    await app.connect();
    statusEl.textContent = "ready";
    askBtn.removeAttribute("disabled");
    askBtn.addEventListener("click", async () => {
      try {
        await app.sendMessage({
          role: "user",
          content: [{ type: "text", text: "Tell me a short joke." }],
        });
        statusEl.textContent = "prompt sent";
      } catch (err) {
        statusEl.textContent = "send err: " + (err?.message ?? err);
      }
    });
  } catch (err) {
    statusEl.textContent = "init err: " + (err?.message ?? err);
  }
}
