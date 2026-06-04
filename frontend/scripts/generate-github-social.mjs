// Renders a 1280×640 GitHub "Social preview" image (the size GitHub
// recommends for repo link cards) → frontend/public/github-social-preview.png.
//
// DRY: it reuses the exact design in scripts/og-card.svg (the 1200×630 OG
// card) — it strips that card's background, then re-lays its shield +
// wordmark, centered, onto a full-bleed 1280×640 canvas with the same
// white background + blue accent bar. No stretching; the shield/wordmark
// path + text live only in og-card.svg.
//
// Upload the PNG at: github.com/<owner>/<repo> -> Settings -> General ->
// "Social preview". Regenerate with `make github-social`.
import puppeteer from 'puppeteer'
import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const OG_CARD = resolve(SCRIPT_DIR, 'og-card.svg')
const OUT = resolve(SCRIPT_DIR, '../public/github-social-preview.png')
const W = 1280, H = 640
const SRC_W = 1200, SRC_H = 630   // og-card.svg's native size

const og = await readFile(OG_CARD, 'utf8')
// Inner content = everything after the blue accent-bar <rect>, up to </svg>
// (i.e. the shield <g> + the wordmark/tagline <text> elements).
const BAR = '<rect x="0" y="0" width="1200" height="8" fill="#2563EB"/>'
const i = og.indexOf(BAR)
if (i < 0) throw new Error('og-card.svg layout changed: accent-bar rect not found')
const inner = og.slice(i + BAR.length, og.lastIndexOf('</svg>'))

const dx = (W - SRC_W) / 2, dy = (H - SRC_H) / 2   // center the 1200×630 content
const card = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
  <rect width="${W}" height="${H}" fill="#FFFFFF"/>
  <rect x="0" y="0" width="${W}" height="8" fill="#2563EB"/>
  <g transform="translate(${dx}, ${dy})">${inner}</g>
</svg>`

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; padding: 0; background: #fff; } svg { display: block; }
</style></head><body>${card}</body></html>`

const browser = await puppeteer.launch({ headless: true })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 })
  await page.setContent(html, { waitUntil: 'networkidle0' })
  const png = await page.screenshot({ type: 'png', clip: { x: 0, y: 0, width: W, height: H } })
  await writeFile(OUT, png)
  console.log(`Wrote ${OUT} (${W}×${H})`)
} finally {
  await browser.close()
}
