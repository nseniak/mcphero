import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@^1/app-with-deps";

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
      min-height: 100vh; min-width: 100vw;
      box-sizing: border-box; padding: 48px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(135deg, #111827 0%, #1e3a8a 100%);
      color: #fff;
      display: flex; flex-direction: column; gap: 24px;
      align-items: center; justify-content: center; text-align: center;
    }
    h1 { font-size: 40px; margin: 0; color: #fbbf24; }
    p  { font-size: 18px; margin: 0; max-width: 640px; line-height: 1.5; color: #e5e7eb; }
    button {
      padding: 18px 32px; font-size: 18px; font-weight: 600;
      border: 0; border-radius: 12px; background: #fbbf24; color: #111827; cursor: pointer;
      box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3);
    }
    button:hover   { background: #f59e0b; }
    button:disabled { background: #9ca3af; cursor: not-allowed; }
    #status { font-size: 12px; color: #9ca3af; }
  `;
  document.head.appendChild(style);

  const statusEl = el("div", { id: "status", text: "connecting…" });
  const askBtn = el("button", { id: "ask", disabled: true, text: "Tell me a joke" });
  root.append(
    el("h1", { text: "MCP Hero demo — fullscreen" }),
    el("p", {
      text:
        "This widget requested fullscreen mode on connect. Click the button "
        + "to post a follow-up prompt back into the conversation.",
    }),
    askBtn,
    statusEl,
  );

  const app = new App({
    name: "mcphero-fullscreen-demo",
    version: "1.0.0",
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
  });

  try {
    await app.connect();
    const ctx = app.getHostContext?.() ?? {};
    const modes = ctx.availableDisplayModes ?? [];
    if (modes.includes("fullscreen")) {
      try {
        await app.requestDisplayMode({ mode: "fullscreen" });
        statusEl.textContent = "fullscreen granted";
      } catch (err) {
        statusEl.textContent = "fullscreen denied: " + (err?.message ?? err);
      }
    } else {
      statusEl.textContent = "fullscreen not offered (modes: " + JSON.stringify(modes) + ")";
    }
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
