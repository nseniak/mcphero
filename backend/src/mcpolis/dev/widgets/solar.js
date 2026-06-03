// Fullscreen solar-system widget.
// Clicking a planet:
//   1. Generates a request_id.
//   2. Subscribes to that request_id over a WebSocket to /ws/explanations.
//   3. Calls record_planet_click(planet_id, request_id) so the model
//      can later retrieve the selection via get_last_clicked_planet.
//   4. When the server pushes the explanation back on the WS, renders
//      a tooltip next to the clicked planet.

import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@^1/app-with-deps";

const PUBLIC_URL = "__PUBLIC_URL__";

function svg(tag, props = {}, kids = []) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, String(v));
  }
  for (const k of kids) e.appendChild(k);
  return e;
}

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

const PLANETS = [
  { id: "Mercury", d: 110, r: 8,  color: "#9ca3af" },
  { id: "Venus",   d: 160, r: 13, color: "#d97706" },
  { id: "Earth",   d: 215, r: 14, color: "#2563eb" },
  { id: "Mars",    d: 270, r: 11, color: "#dc2626" },
  { id: "Jupiter", d: 360, r: 30, color: "#ca8a04" },
  { id: "Saturn",  d: 450, r: 26, color: "#eab308" },
  { id: "Uranus",  d: 530, r: 18, color: "#06b6d4" },
  { id: "Neptune", d: 600, r: 17, color: "#1d4ed8" },
];

export default async function mount(root) {
  const style = document.createElement("style");
  style.textContent = `
    html, body {
      margin: 0; padding: 0;
      width: 100vw; height: 100vh;
      overflow: hidden;
      overscroll-behavior: none;
      touch-action: none;
      background: #030712; color: #f9fafb;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    #scene { position: fixed; inset: 0; overflow: hidden; }
    svg { display: block; width: 100vw; height: 100vh; cursor: grab;
          touch-action: none; user-select: none; -webkit-user-select: none;
          -webkit-user-drag: none; }
    svg.panning { cursor: grabbing; }
    .planet { cursor: pointer; transition: filter .15s; }
    .planet:hover { filter: brightness(1.4) drop-shadow(0 0 8px rgba(255,255,255,0.6)); }
    .orbit { fill: none; stroke: rgba(255,255,255,0.08); stroke-width: 1; }
    .label { font-size: 11px; fill: rgba(255,255,255,0.55); user-select: none; pointer-events: none; }
    #header {
      position: fixed; top: 20px; left: 24px; z-index: 2; pointer-events: none;
    }
    #header h1 { margin: 0 0 4px 0; font-size: 20px; color: #fbbf24; }
    #header p  { margin: 0; font-size: 12px; color: #9ca3af; }
    .tooltip {
      position: fixed; max-width: 320px; padding: 12px 14px;
      background: rgba(17, 24, 39, 0.95); border: 1px solid #fbbf24;
      border-radius: 10px; color: #f9fafb; font-size: 13px; line-height: 1.45;
      box-shadow: 0 10px 30px -5px rgba(0,0,0,0.5);
      z-index: 10; pointer-events: auto;
    }
    .tooltip .title { font-size: 13px; font-weight: 700; color: #fbbf24; margin: 0 0 6px 0; }
    .tooltip .body  { margin: 0; }
    .tooltip .close {
      position: absolute; top: 4px; right: 8px; background: transparent; color: #9ca3af;
      border: 0; font-size: 14px; cursor: pointer;
    }
    .tooltip.loading .body::after {
      content: "…"; display: inline-block; animation: dots 1.2s infinite;
    }
    @keyframes dots { 0%,20% {content:"."} 40% {content:".."} 60%,100% {content:"..."} }
  `;
  document.head.appendChild(style);

  const header = el("div", { id: "header" });
  header.append(
    el("h1", { text: "Solar system" }),
    el("p",  { text: 'Click a planet, then type "explain" in the chat.' }),
  );
  root.appendChild(header);

  const scene = el("div", { id: "scene" });
  root.appendChild(scene);

  const hint = el("div", {
    class: "hint",
    style: "position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); "
         + "font-size: 11px; color: #6b7280; pointer-events: none; user-select: none;",
    text: 'drag to pan · click a planet then type "explain" in the chat',
  });
  root.appendChild(hint);

  const SVG_NS = "http://www.w3.org/2000/svg";
  const root_svg = svg("svg", { xmlns: SVG_NS });
  scene.appendChild(root_svg);

  const bgRect = svg("rect", {
    x: -10000, y: -10000, width: 20000, height: 20000, fill: "transparent",
  });
  root_svg.appendChild(bgRect);

  const universe = svg("g");
  root_svg.appendChild(universe);

  const STORAGE_KEY = "mcphero/solar/pan";
  let panX = 0;
  let panY = 0;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const p = JSON.parse(saved);
      if (typeof p?.x === "number" && typeof p?.y === "number") {
        panX = p.x; panY = p.y;
      }
    }
  } catch (_) { /* sandbox may block storage */ }

  let saveTimer = null;
  function savePan() {
    if (saveTimer) return;
    saveTimer = setTimeout(() => {
      saveTimer = null;
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ x: panX, y: panY })); }
      catch (_) {}
    }, 150);
  }

  function applyPan() {
    universe.setAttribute("transform", `translate(${panX} ${panY})`);
    savePan();
  }

  function layout() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    root_svg.setAttribute("viewBox", `${-w / 2} ${-h / 2} ${w} ${h}`);
  }
  layout();
  window.addEventListener("resize", layout);

  applyPan();

  for (const p of PLANETS) {
    universe.appendChild(svg("circle", { class: "orbit", cx: 0, cy: 0, r: p.d }));
  }

  universe.appendChild(svg("circle", { cx: 0, cy: 0, r: 40, fill: "#fbbf24" }));
  const sunLabel = svg("text", { class: "label", x: 0, y: 60, "text-anchor": "middle" });
  sunLabel.textContent = "Sun";
  universe.appendChild(sunLabel);

  const angleFor = (i) => (i / PLANETS.length) * Math.PI * 2 + Math.PI / 6;
  const planetNodes = PLANETS.map((p, i) => {
    const a = angleFor(i);
    const x = Math.cos(a) * p.d;
    const y = Math.sin(a) * p.d;
    const circle = svg("circle", {
      class: "planet", cx: x, cy: y, r: p.r, fill: p.color,
      "data-id": p.id,
    });
    const label = svg("text", {
      class: "label", x, y: y + p.r + 16, "text-anchor": "middle",
    });
    label.textContent = p.id;
    universe.append(circle, label);
    return { planet: p, circle, x, y };
  });

  const app = new App({
    name: "mcphero-solar-demo", version: "1.0.0",
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
  });
  try {
    await app.connect();
    const modes = app.getHostContext?.()?.availableDisplayModes ?? [];
    if (modes.includes("fullscreen")) {
      try { await app.requestDisplayMode({ mode: "fullscreen" }); } catch (_) {}
    }
  } catch (_) { /* widget works standalone too */ }

  const wsUrl = PUBLIC_URL.replace(/^http/, "ws") + "/dev/mcp-demo/ws/explanations";
  let ws = null;
  let retryMs = 500;
  const pending = new Map();

  function openWs() {
    ws = new WebSocket(wsUrl);
    ws.onopen = () => { retryMs = 500; };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg?.type !== "explanation") return;
      const p = pending.get(msg.request_id);
      if (!p) return;
      pending.delete(msg.request_id);
      p.tooltipEl.classList.remove("loading");
      p.bodyEl.textContent = msg.text || "(empty response)";
    };
    ws.onclose = () => {
      setTimeout(openWs, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
  }
  openWs();

  const DRAG_THRESHOLD = 5;
  let drag = null;

  root_svg.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    e.preventDefault();
    e.stopPropagation();
    drag = {
      startX: e.clientX, startY: e.clientY,
      startPanX: panX, startPanY: panY,
      active: false, pointerId: e.pointerId,
    };
  }, { capture: true });

  window.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (!drag.active) {
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      drag.active = true;
      root_svg.classList.add("panning");
      try { root_svg.setPointerCapture(drag.pointerId); } catch (_) {}
    }
    e.preventDefault();
    e.stopPropagation();
    panX = drag.startPanX + dx;
    panY = drag.startPanY + dy;
    applyPan();
    repositionTooltips();
  }, { capture: true, passive: false });

  const killNative = (e) => { e.preventDefault(); e.stopPropagation(); };
  window.addEventListener("wheel",       killNative, { capture: true, passive: false });
  window.addEventListener("touchmove",   killNative, { capture: true, passive: false });
  window.addEventListener("dragstart",   killNative, { capture: true });
  window.addEventListener("selectstart", killNative, { capture: true });
  window.addEventListener("gesturestart", killNative, { capture: true });

  function endDrag(e) {
    if (!drag) return;
    if (e && e.pointerId !== drag.pointerId) return;
    if (drag.active) {
      root_svg.classList.remove("panning");
      try { root_svg.releasePointerCapture(drag.pointerId); } catch (_) {}
      const killClick = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
      window.addEventListener("click", killClick, { capture: true, once: true });
      setTimeout(() => window.removeEventListener("click", killClick, { capture: true }), 0);
    }
    drag = null;
  }
  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);

  root_svg.addEventListener("click", (e) => {
    if (e.target === root_svg || e.target.tagName === "g") {
      for (const t of document.querySelectorAll(".tooltip")) t.remove();
    }
  });

  const tooltipAnchors = new Map();
  function positionTooltip(tip, circle) {
    const bbox = circle.getBoundingClientRect();
    tip.style.left = `${Math.min(window.innerWidth - 340, bbox.right + 12)}px`;
    tip.style.top  = `${Math.max(16, bbox.top - 20)}px`;
  }
  function repositionTooltips() {
    for (const [tip, circle] of tooltipAnchors) positionTooltip(tip, circle);
  }

  for (const { planet, circle } of planetNodes) {
    circle.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      for (const t of document.querySelectorAll(`.tooltip[data-id="${planet.id}"]`)) {
        tooltipAnchors.delete(t);
        t.remove();
      }

      const request_id = crypto.randomUUID();

      const tip = el("div", {
        class: "tooltip loading", "data-id": planet.id,
      });
      tooltipAnchors.set(tip, circle);
      const body = el("p", {
        class: "body",
        text: 'type "explain" in the chat to get an explanation',
      });
      const close = el("button", { class: "close", text: "×" });
      close.addEventListener("click", () => {
        tooltipAnchors.delete(tip);
        tip.remove();
        pending.delete(request_id);
      });
      tip.append(
        el("p", { class: "title", text: planet.id }),
        close,
        body,
      );
      document.body.appendChild(tip);
      positionTooltip(tip, circle);
      pending.set(request_id, { tooltipEl: tip, bodyEl: body });

      const sub = JSON.stringify({ subscribe: request_id });
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(sub);
      else ws?.addEventListener?.("open", () => ws.send(sub), { once: true });

      try {
        await app.callServerTool({
          name: "record_planet_click",
          arguments: { planet_id: planet.id, request_id },
        });
      } catch (err) {
        tip.classList.remove("loading");
        body.textContent = "record err: " + (err?.message ?? err);
      }
    });
  }
}
