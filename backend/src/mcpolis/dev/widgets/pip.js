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
      width: 320px; height: 220px;
      box-sizing: border-box; padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #ecfeff; border: 8px solid #0891b2; color: #111;
      display: flex; flex-direction: column; gap: 12px; justify-content: space-between;
    }
    h1 { font-size: 20px; margin: 0; color: #0e7490; }
    p  { font-size: 13px; margin: 0; line-height: 1.4; }
    code { font-family: ui-monospace, monospace; background: rgba(0,0,0,0.05); padding: 0 3px; border-radius: 3px; }
    button {
      align-self: flex-start; padding: 10px 16px; font-size: 14px;
      border: 0; border-radius: 10px; background: #0891b2; color: #fff; cursor: pointer;
    }
    button:hover    { background: #0e7490; }
    button:disabled { background: #9ca3af; cursor: not-allowed; }
    #status { font-size: 11px; color: #555; }
  `;
  document.head.appendChild(style);

  const p = document.createElement("p");
  p.appendChild(document.createTextNode(
    "Requests floating picture-in-picture mode on connect. Falls back to "
    + "inline when the host doesn't offer ",
  ));
  p.appendChild(el("code", { text: "pip" }));
  p.appendChild(document.createTextNode("."));

  const statusEl = el("div", { id: "status", text: "connecting…" });
  const askBtn = el("button", { id: "ask", disabled: true, text: "Tell me a joke" });
  root.append(
    el("h1", { text: "MCP Hero demo — PiP" }),
    p,
    askBtn,
    statusEl,
  );

  const app = new App({
    name: "mcphero-pip-demo",
    version: "1.0.0",
    appCapabilities: { availableDisplayModes: ["inline", "pip"] },
  });

  try {
    await app.connect();
    const ctx = app.getHostContext?.() ?? {};
    const modes = ctx.availableDisplayModes ?? [];
    if (modes.includes("pip")) {
      try {
        await app.requestDisplayMode({ mode: "pip" });
        statusEl.textContent = "floating (pip)";
      } catch (err) {
        statusEl.textContent = "pip denied: " + (err?.message ?? err);
      }
    } else {
      statusEl.textContent = "inline · host offers " + JSON.stringify(modes);
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
