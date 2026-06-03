// Renders frontend/scripts/og-card.svg → frontend/public/og-image.png
// at the OG-card-standard 1200×630 size. Uses the puppeteer that ships
// transitively via @prerenderer/renderer-puppeteer (no extra dependency).
//
// Run from the repo root with `make og-image`, or directly:
//   cd frontend && node scripts/generate-og-image.mjs
//
// Re-run whenever you edit og-card.svg. The PNG is what social platforms
// (LinkedIn, Slack, iMessage, Discord, etc.) actually render — the SVG is
// the source-of-truth that humans edit.
import puppeteer from 'puppeteer'
import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const SVG_PATH = resolve(SCRIPT_DIR, 'og-card.svg')
const PNG_PATH = resolve(SCRIPT_DIR, '../public/og-image.png')
const WIDTH = 1200
const HEIGHT = 630

const svg = await readFile(SVG_PATH, 'utf8')
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; padding: 0; background: #fff; }
  svg { display: block; }
</style></head><body>${svg}</body></html>`

const browser = await puppeteer.launch({ headless: true })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 })
  await page.setContent(html, { waitUntil: 'networkidle0' })
  const png = await page.screenshot({
    type: 'png',
    clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT },
  })
  await writeFile(PNG_PATH, png)
  console.log(`Wrote ${PNG_PATH} (${WIDTH}×${HEIGHT})`)
} finally {
  await browser.close()
}
