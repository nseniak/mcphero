import { defineConfig, loadEnv, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import prerender from '@prerenderer/rollup-plugin'
import PuppeteerRenderer from '@prerenderer/renderer-puppeteer'
import { readFile, writeFile, rename } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Public files that aren't index.html and therefore aren't covered by Vite's
// native `%VITE_FOO%` substitution. Templated by `publicEnvSubstitution` in
// dev (middleware) and after build (rewrite the copied dist files).
const TEMPLATED_PUBLIC_FILES = ['/sitemap.xml', '/robots.txt']

function publicEnvSubstitution(values: Record<string, string>): Plugin {
  const substitute = (content: string): string => {
    let out = content
    for (const [key, value] of Object.entries(values)) {
      out = out.replaceAll(`%${key}%`, value)
    }
    return out
  }
  return {
    name: 'mcpolis-public-env-substitution',
    // Run before Vite's built-in HTML env replacement so we can substitute
    // `%VITE_FOO%` placeholders with our fallback-aware values. Without this,
    // a missing env var would leave the placeholder literal in `index.html`.
    transformIndexHtml: {
      order: 'pre' as const,
      handler: substitute,
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url?.split('?')[0]
        if (!url || !TEMPLATED_PUBLIC_FILES.includes(url)) return next()
        try {
          const file = resolve(server.config.publicDir, url.slice(1))
          const content = readFileSync(file, 'utf8')
          res.setHeader('Content-Type', url.endsWith('.xml') ? 'application/xml' : 'text/plain')
          res.end(substitute(content))
        } catch {
          next()
        }
      })
    },
    async closeBundle() {
      for (const path of TEMPLATED_PUBLIC_FILES) {
        const file = resolve('dist', path.slice(1))
        try {
          const content = await readFile(file, 'utf8')
          await writeFile(file, substitute(content))
        } catch {
          // file not in build output — ignore
        }
      }
    },
  }
}

// Public marketing routes prerendered at build time so non-JS crawlers
// (LinkedIn, Slack-mobile, AI ingestion bots) see real HTML — not the empty
// SPA shell. Authenticated app routes (`/app`, `/orgs/...`) are deliberately
// excluded; they fall through to the SPA shell.
const PRERENDER_ROUTES = [
  '/',
  '/pricing',
  '/privacy',
  '/security',
  '/terms',
  '/contact',
  '/docs',
  '/docs/concepts',
  '/docs/getting-started',
  '/docs/upstream-mcps',
  '/docs/team',
  '/docs/roles-and-permissions',
  '/docs/audit-log',
  '/docs/admin-mcp',
]

// Captured by the prerenderer's `pageHandler` for `/` and written in
// `writeBundle`. This bypasses a rolldown quirk where the prerender plugin's
// re-emit of `index.html` (after deleting the original from the bundle) is
// silently dropped during bundle finalization, leaving `dist/index.html`
// missing. The other routes write to `dist/<route>/index.html` and aren't
// affected.
let homeRenderedHtml: string | null = null

const writeHomeIndexPlugin: Plugin = {
  name: 'mcpolis-write-home-index',
  apply: 'build',
  enforce: 'post',
  async writeBundle(options) {
    if (!homeRenderedHtml) return
    const outDir = options.dir ?? 'dist'
    const target = resolve(outDir, 'index.html')
    await writeFile(target, homeRenderedHtml.trim() + '\n', 'utf8')
    // Keep the vanilla Vite shell as `_app.html` for nginx to use as the
    // SPA fallback on deep app routes — without this the prerendered
    // marketing HTML at `/index.html` gets served for every unknown path
    // and paints for a frame before React mounts and replaces it.
    await rename(
      resolve(outDir, 'index_fallback.html'),
      resolve(outDir, '_app.html'),
    )
  },
}

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  // Site URL drives canonical/og:url/og:image, sitemap.xml, and robots.txt.
  // Loaded with a hard-coded fallback so a missing env var doesn't ship
  // literal `%VITE_SITE_URL%` placeholders to production.
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const siteUrl = env.VITE_SITE_URL || 'https://mcphero.io'
  // Backend host:port the Vite proxy forwards /api etc. to. Defaults
  // to the canonical dev port; the e2e sharding orchestrator (see
  // tests/run-e2e-tests.py) overrides via env so each shard's
  // frontend talks to its own backend instance.
  const backendHost = process.env.MCPOLIS_BACKEND_HOST || 'localhost'
  const backendPort = process.env.MCPOLIS_BACKEND_PORT || '8080'
  const backendHttp = `http://${backendHost}:${backendPort}`
  const backendWs = `ws://${backendHost}:${backendPort}`
  return {
  plugins: [
    react(),
    tailwindcss(),
    publicEnvSubstitution({ VITE_SITE_URL: siteUrl }),
    ...(command === 'build'
      ? [
          prerender({
            routes: PRERENDER_ROUTES,
            // Keep the original Vite-generated index.html as `_fallback.html`.
            // Without this, rolldown's bundle finalization silently drops the
            // re-emitted `index.html` produced by the prerenderer for `/`.
            // We delete the fallback in `writeBundle` after using it.
            fallback: true,
            renderer: new PuppeteerRenderer({
              renderAfterDocumentEvent: 'render-event',
              headless: true,
              // Backend API calls (/api/features, /api/auth) 404 against the
              // tiny static server the prerenderer spins up. Skipping
              // third-party requests prevents puppeteer from waiting on
              // Sentry / analytics / API requests that never resolve.
              skipThirdPartyRequests: true,
              maxConcurrentRoutes: 1,
              timeout: 60000,
              // Sets `window.__PRERENDER_INJECTED = true` in the headless
              // browser so app code can detect prerender context — used by
              // HomePage to skip the redirect-to-/app branch and render its
              // marketing content instead.
              inject: true,
              consoleHandler: (route, message) => {
                if (message.type() === 'error') {
                  console.warn(`[prerender ${route}] error:`, message.text())
                }
              },
              pageHandler: async (page, route) => {
                if (route === '/') {
                  homeRenderedHtml = await page.content()
                }
              },
            }),
          }),
          writeHomeIndexPlugin,
        ]
      : []),
  ],
  server: {
    // When serving the dev server behind a proxy or tunnel, set
    // MCPOLIS_DEV_ALLOWED_HOSTS to the external host(s), comma-separated
    // (e.g. MCPOLIS_DEV_ALLOWED_HOSTS=dev.example.com). Empty = localhost
    // only, which is Vite's default-secure behaviour.
    allowedHosts: (process.env.MCPOLIS_DEV_ALLOWED_HOSTS ?? '')
      .split(',').map((h) => h.trim()).filter(Boolean),
    fs: {
      // Allow Vite to read repo-root markdown (../docs/*.md) imported by the
      // /docs pages. The frontend has no workspace-root package.json above it
      // for searchForWorkspaceRoot() to find, so we widen the allow list
      // explicitly to the repo root (one level up from `frontend/`).
      allow: ['.', '..'],
    },
    proxy: {
      '/api': backendHttp,
      '/oauth': backendHttp,
      '/mcp': backendHttp,
      '/admin-mcp': backendHttp,
      '/.well-known': backendHttp,
      // Bundled demo upstream + future dev mounts. WebSockets need an
      // explicit ws:// target with `ws: true` so Vite forwards the
      // upgrade — without this entry the counter / solar widgets
      // would 404 on connect when running through the Vite proxy.
      '/dev/mcp-demo/ws': {
        target: backendWs,
        ws: true,
      },
      '/dev': backendHttp,
    },
  },
  }
})
