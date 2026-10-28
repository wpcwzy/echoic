#!/usr/bin/env node
/**
 * Render Echoic PWA icons from the in-app brand mark (v1.0.4).
 *
 * The logo is the same one used by <BrandMark>: a rounded tile filled with the
 * brand gradient (`#1677ff → #3f8cff → #6aa9ff`, 135°) and six white rounded
 * sound-wave bars.  Keeping the generator in the repo means the install icons
 * can never drift away from the in-app logo again.
 *
 * Usage (from the repo root, Playwright is available in e2e/):
 *   PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright node scripts/render_pwa_icons.mjs
 *
 * Outputs (frontend/public/):
 *   favicon.svg                        rounded tile, 64×64 (vector favicon)
 *   icons/icon-192.png                 purpose "any"          (rounded, transparent corners)
 *   icons/icon-512.png                 purpose "any"
 *   icons/icon-maskable-512.png        purpose "maskable"     (full-bleed, glyph in safe zone)
 *   icons/apple-touch-icon.png         180×180 full-bleed    (iOS applies its own mask)
 *   icons/favicon-32.png               32×32 rounded
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '../e2e/node_modules/playwright/index.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PUBLIC = resolve(ROOT, 'frontend/public')
const ICONS = resolve(PUBLIC, 'icons')

const GRADIENT = ['#1677ff', '#3f8cff', '#6aa9ff']
/** Same geometry/order as frontend/src/components/common/BrandMark.tsx. */
const BARS = [
  { x: 6, h: 8 },
  { x: 10, h: 14 },
  { x: 14, h: 20 },
  { x: 18, h: 12 },
  { x: 22, h: 17 },
  { x: 26, h: 9 },
]

/** Sound-wave glyph as an SVG group (32×32 design space, vertically centred). */
function glyph({ scale = 1 } = {}) {
  const bars = BARS.map(
    ({ x, h }) =>
      `<rect x="${x - 1.4}" y="${16 - h / 2}" width="2.8" height="${h}" rx="1.4" fill="#ffffff" fill-opacity="0.96"/>`,
  ).join('')
  // Scale around the glyph centre (16,16) so it always stays centred.
  const offset = (32 * (1 - scale)) / 2
  return `<g transform="translate(${offset} ${offset}) scale(${scale})">${bars}</g>`
}

function svg({ radius = 0, scale = 1, size = 512 } = {}) {
  const [a, b, c] = GRADIENT
  const tile =
    radius > 0
      ? `<rect x="0" y="0" width="32" height="32" rx="${radius}" fill="url(#brand)"/>`
      : `<rect x="0" y="0" width="32" height="32" fill="url(#brand)"/>`
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${a}"/>
      <stop offset="0.55" stop-color="${b}"/>
      <stop offset="1" stop-color="${c}"/>
    </linearGradient>
  </defs>
  ${tile}
  ${glyph({ scale })}
</svg>`
}

const TARGETS = [
  // "any" icons: rounded like the in-app BrandMark tile, transparent corners.
  { file: 'icons/icon-192.png', size: 192, radius: 32 * 0.3, scale: 1 },
  { file: 'icons/icon-512.png', size: 512, radius: 32 * 0.3, scale: 1 },
  // Maskable: full-bleed background, glyph inside the central 80% safe zone.
  { file: 'icons/icon-maskable-512.png', size: 512, radius: 0, scale: 0.72 },
  // iOS masks the corners itself, so keep the gradient edge-to-edge.
  { file: 'icons/apple-touch-icon.png', size: 180, radius: 0, scale: 0.78 },
  { file: 'icons/favicon-32.png', size: 32, radius: 32 * 0.3, scale: 1 },
]

async function main() {
  await mkdir(ICONS, { recursive: true })
  const browser = await chromium.launch()
  const page = await browser.newPage()

  for (const target of TARGETS) {
    const markup = svg({ radius: target.radius, scale: target.scale, size: target.size })
    await page.setViewportSize({ width: target.size, height: target.size })
    await page.setContent(
      `<html><body style="margin:0;background:transparent">${markup}</body></html>`,
      { waitUntil: 'load' },
    )
    const element = await page.locator('svg').boundingBox()
    const png = await page.screenshot({
      omitBackground: true,
      clip: { x: element.x, y: element.y, width: target.size, height: target.size },
    })
    await writeFile(resolve(PUBLIC, target.file), png)
    console.log(`rendered ${target.file} (${target.size}×${target.size}, ${png.length} bytes)`)
  }

  // Vector favicon (same mark, rounded tile) so browsers and other surfaces
  // that prefer SVG pick up the logo too.
  const favicon = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${GRADIENT[0]}"/>
      <stop offset="0.55" stop-color="${GRADIENT[1]}"/>
      <stop offset="1" stop-color="${GRADIENT[2]}"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="32" height="32" rx="9.6" fill="url(#brand)"/>
  ${glyph()}
</svg>
`
  await writeFile(resolve(PUBLIC, 'favicon.svg'), favicon)
  console.log('wrote favicon.svg')

  await browser.close()

  // Sanity check: PNG signatures + IHDR dimensions.
  for (const target of TARGETS) {
    const buffer = await (await import('node:fs/promises')).readFile(resolve(PUBLIC, target.file))
    if (buffer.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') {
      throw new Error(`${target.file} is not a PNG`)
    }
    const width = buffer.readUInt32BE(16)
    const height = buffer.readUInt32BE(20)
    if (width !== target.size || height !== target.size) {
      throw new Error(`${target.file} is ${width}×${height}, expected ${target.size}×${target.size}`)
    }
  }
  console.log('all icons verified')
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})