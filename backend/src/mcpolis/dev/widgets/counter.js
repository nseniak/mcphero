import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@^1/app-with-deps";

// __PUBLIC_URL__ is substituted server-side before serving this file.
const PUBLIC_URL = "__PUBLIC_URL__";

function el(tag, props = {}, kids = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") e.textContent = String(v);
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
      width: 420px; height: 260px;
      box-sizing: border-box; padding: 28px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f0fdf4; border: 8px solid #15803d; color: #111;
      display: flex; flex-direction: column; gap: 12px; justify-content: space-between;
    }
    h1 { font-size: 18px; margin: 0; color: #166534; }
    p  { font-size: 12px; margin: 0; color: #333; }
    #counter {
      font-family: ui-monospace, monospace; font-size: 72px; font-weight: 700;
      color: #15803d; text-align: center; margin: 0; line-height: 1;
    }
    #status { font-size: 11px; color: #555; }
  `;
  document.head.appendChild(style);

  const counterEl = el("div", { id: "counter", text: "—" });
  const statusEl = el("div", { id: "status", text: "connecting…" });
  root.append(
    el("h1", { text: "Counter streamed from the backend" }),
    el("p", {
      text:
        "Backend pushes an incrementing integer over a WebSocket. The widget "
        + "receives each tick and updates the number below.",
    }),
    counterEl,
    statusEl,
  );

  const app = new App({ name: "mcphero-counter-demo", version: "1.0.0" });
  try { await app.connect(); } catch (_) { /* stream works independently */ }

  const wsUrl = PUBLIC_URL.replace(/^http/, "ws") + "/dev/mcp-demo/ws/counter";
  let retryMs = 500;

  function connect() {
    statusEl.textContent = "ws: connecting…";
    const ws = new WebSocket(wsUrl);
    ws.onopen    = () => { statusEl.textContent = "ws: open"; retryMs = 500; };
    ws.onmessage = (e) => {
      counterEl.textContent = e.data;
      statusEl.textContent  = "ws: tick " + new Date().toLocaleTimeString();
    };
    ws.onclose = () => {
      statusEl.textContent = `ws: closed — reconnecting in ${retryMs}ms`;
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
    ws.onerror = () => { /* onclose will follow */ };
  }
  connect();
}
