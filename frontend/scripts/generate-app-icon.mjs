// Generate frontend/public/apple-touch-icon.png — a square, TRANSPARENT app
// icon rendered from the shield (public/favicon.svg). It's referenced by
// <link rel="apple-touch-icon"> so connector-icon fetchers (e.g. Claude's
// MCP connector list) get a proper transparent square icon instead of the
// opaque 1200×630 OG card. Uses the puppeteer that ships transitively via
// @prerenderer/renderer-puppeteer (no extra dependency).
//
// Regenerate after editing favicon.svg:  make app-icon   (or: node scripts/generate-app-icon.mjs)
import puppeteer from 'puppeteer'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const SIZE = 512
const svg = await readFile(resolve(SCRIPT_DIR, '../public/favicon.svg'), 'utf8')

// Center the (already-square, already-padded) shield in a transparent canvas.
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; padding: 0; background: transparent; }
  #wrap { width: ${SIZE}px; height: ${SIZE}px; display: flex;
          align-items: center; justify-content: center; }
  #wrap svg { width: 100%; height: 100%; }
</style></head><body><div id="wrap">${svg}</div></body></html>`

const browser = await puppeteer.launch({ headless: true })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: SIZE, height: SIZE, deviceScaleFactor: 1 })
  await page.setContent(html, { waitUntil: 'networkidle0' })
  const el = await page.$('#wrap')
  const out = resolve(SCRIPT_DIR, '../public/apple-touch-icon.png')
  await el.screenshot({ path: out, omitBackground: true }) // omitBackground => transparent
  console.log('wrote', out)
} finally {
  await browser.close()
}
