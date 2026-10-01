import { test, expect } from "@playwright/test";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { createRequire } from "node:module";

/**
 * The dev Vite proxy's connection pool, exercised with the REAL
 * frontend/vite.config.ts against a small backend that counts its TCP
 * connections. Node-side only: no browser, no app backend.
 *
 * Why the pool exists: without it every API call opened two new loopback
 * connections, and the e2e run's bursts made macOS stall every new
 * loopback connection for seconds (see the "Loopback rule" in CLAUDE.md).
 * These checks pin what the pool must and must not change.
 */

const FRONTEND_DIR = path.resolve(__dirname, "../../frontend");

type ViteDevServer = {
  listen(): Promise<unknown>;
  close(): Promise<void>;
};
type Vite = { createServer(config: object): Promise<ViteDevServer> };

type Backend = {
  port: number;
  connections: () => number;
  openSockets: () => number;
  endlessStreamClosed: Promise<void>;
  close: () => Promise<void>;
};

async function freePort(): Promise<number> {
  const probe = net.createServer();
  await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const { port } = probe.address() as net.AddressInfo;
  await new Promise<void>((resolve) => probe.close(() => resolve()));
  return port;
}

async function makeBackend(): Promise<Backend> {
  let connections = 0;
  const sockets = new Set<net.Socket>();
  let markEndlessClosed: () => void = () => {};
  const endlessStreamClosed = new Promise<void>((resolve) => {
    markEndlessClosed = resolve;
  });
  const server = http.createServer((req, res) => {
    if (req.url === "/api/ping") {
      res.setHeader("content-type", "application/json");
      res.end("{}");
    } else if (req.url === "/api/quiet-stream") {
      // Two events 3 s apart: longer than the pool's 2 s idle timeout.
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write("data: first\n\n");
      setTimeout(() => res.end("data: second\n\n"), 3000);
    } else if (req.url === "/api/endless-stream") {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write("data: first\n\n");
      res.on("close", () => markEndlessClosed());
    } else if (req.url === "/api/early-reject") {
      // Like uvicorn answering a 401 before reading a large upload: stop
      // reading the body (uvicorn pauses once 64 KiB sit unread), answer,
      // then read again, so a close from the proxy is noticed.
      req.on("data", () => {});
      req.pause();
      res.writeHead(401, { "content-type": "text/plain" });
      res.end("no", () => req.resume());
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  server.keepAliveTimeout = 5000; // uvicorn's default
  server.on("connection", (socket) => {
    connections += 1;
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    port: (server.address() as net.AddressInfo).port,
    connections: () => connections,
    openSockets: () => sockets.size,
    endlessStreamClosed,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}

async function startVite(backendPort: number, port: number): Promise<ViteDevServer> {
  const previous = {
    host: process.env.MCPOLIS_BACKEND_HOST,
    port: process.env.MCPOLIS_BACKEND_PORT,
  };
  // vite.config.ts reads its proxy target from these when it loads.
  process.env.MCPOLIS_BACKEND_HOST = "127.0.0.1";
  process.env.MCPOLIS_BACKEND_PORT = String(backendPort);
  try {
    const vite = createRequire(path.join(FRONTEND_DIR, "package.json"))("vite") as Vite;
    const server = await vite.createServer({
      root: FRONTEND_DIR,
      configFile: path.join(FRONTEND_DIR, "vite.config.ts"),
      logLevel: "silent",
      server: { host: "127.0.0.1", port, strictPort: true, hmr: false },
    });
    await server.listen();
    return server;
  } finally {
    if (previous.host === undefined) delete process.env.MCPOLIS_BACKEND_HOST;
    else process.env.MCPOLIS_BACKEND_HOST = previous.host;
    if (previous.port === undefined) delete process.env.MCPOLIS_BACKEND_PORT;
    else process.env.MCPOLIS_BACKEND_PORT = previous.port;
  }
}

// Browser-like client: keeps its connections to Vite open. The proxy passes
// the client's `Connection` header upstream, so a client sending
// `Connection: close` would defeat the pool on its own.
const browserAgent = new http.Agent({ keepAlive: true });

function get(port: number, urlPath: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: "127.0.0.1", port, path: urlPath, agent: browserAgent }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk: string) => (body += chunk));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
    });
    req.on("error", reject);
  });
}

async function waitFor(check: () => boolean, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (check()) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return check();
}

test.describe("Vite dev proxy connection pool", () => {
  let backend: Backend;
  let vite: ViteDevServer;
  let vitePort: number;

  test.beforeAll(async () => {
    backend = await makeBackend();
    vitePort = await freePort();
    vite = await startVite(backend.port, vitePort);
  });

  test.afterAll(async () => {
    browserAgent.destroy();
    await vite?.close();
    await backend?.close();
  });

  test("sequential API calls share one backend connection", async () => {
    const before = backend.connections();
    for (let i = 0; i < 6; i += 1) {
      const resp = await get(vitePort, "/api/ping");
      expect(resp.status).toBe(200);
    }
    expect(backend.connections() - before).toBe(1);
  });

  test("a stream quieter than the pool timeout still delivers every event", async () => {
    const resp = await get(vitePort, "/api/quiet-stream");
    expect(resp.body).toContain("data: first");
    expect(resp.body).toContain("data: second");
  });

  test("a browser abort closes the backend connection", async () => {
    await new Promise<void>((resolve, reject) => {
      const req = http.get(
        { host: "127.0.0.1", port: vitePort, path: "/api/endless-stream", agent: browserAgent },
        (res) => res.once("data", () => {
          req.destroy();
          resolve();
        }),
      );
      req.on("error", (err: NodeJS.ErrnoException) => {
        if (err.code !== "ECONNRESET") reject(err);
      });
    });
    const closed = await Promise.race([
      backend.endlessStreamClosed.then(() => true),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 3000)),
    ]);
    expect(closed).toBe(true);
  });

  test("an early answer to a large upload leaves no backend connection open", async () => {
    const body = Buffer.alloc(3_000_000, 1);
    await new Promise<void>((resolve) => {
      const req = http.request(
        {
          host: "127.0.0.1",
          port: vitePort,
          path: "/api/early-reject",
          method: "POST",
          agent: browserAgent,
          headers: { "content-type": "application/octet-stream", "content-length": body.length },
        },
        (res) => {
          res.resume();
          res.on("end", () => resolve());
        },
      );
      // The proxy may hang up while the body is still being sent.
      req.on("error", () => resolve());
      req.end(body);
    });
    // The pool closes idle sockets after 2 s; without the fix this one is
    // never idle, so it stays open for good.
    expect(await waitFor(() => backend.openSockets() === 0, 4000)).toBe(true);
  });
});
