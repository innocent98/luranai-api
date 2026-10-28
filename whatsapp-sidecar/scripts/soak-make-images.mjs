// Writes valid PNG files of a chosen size (incompressible noise, no dependencies) for the image-burst phase of the soak.
//   node scripts/soak-make-images.mjs <outDir> [sizeMiB ...]      default sizes: 1 4 9.5   (the sidecar caps images at 10 MiB)
// Use them for the OUTBOUND path (upload to the media host, send from the agent app); a phone re-encodes photos it sends,
// so these files do not stay this size on the INBOUND path. See docs/sop/whatsapp_sidecar_soak.md.
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { crc32, deflateSync } from 'node:zlib'

const WIDTH = 2000

const chunk = (type, data) => {
  const head = Buffer.alloc(8)
  head.writeUInt32BE(data.length, 0); head.write(type, 4, 'latin1')
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0)
  return Buffer.concat([head, data, crc])
}

// Deterministic xorshift noise: the same size always gives the same bytes.
function noise(n, seed) {
  const out = Buffer.allocUnsafe(n)
  let x = seed >>> 0 || 1
  for (let i = 0; i < n; i++) { x ^= x << 13; x >>>= 0; x ^= x >>> 17; x ^= x << 5; x >>>= 0; out[i] = x & 0xff }
  return out
}

// A PNG whose total length is within one image row (about 6 KB) of `targetBytes`.
export function makePng(targetBytes) {
  const rowBytes = WIDTH * 3 + 1
  const height = Math.max(1, Math.round(targetBytes / rowBytes))
  const raw = noise(rowBytes * height, height)
  for (let y = 0; y < height; y++) raw[y * rowBytes] = 0   // filter type None for every row
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(WIDTH, 0); ihdr.writeUInt32BE(height, 4); ihdr[8] = 8; ihdr[9] = 2   // 8-bit RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw, { level: 0 })), chunk('IEND', Buffer.alloc(0)),
  ])
}

function main() {
  const [dir, ...sizes] = process.argv.slice(2)
  if (!dir) { console.error('usage: soak-make-images.mjs <outDir> [sizeMiB ...]'); process.exit(2) }
  const list = (sizes.length ? sizes : ['1', '4', '9.5']).map(Number)
  if (list.some((s) => !(s >= 0.05 && s <= 12))) { console.error('sizes are MiB between 0.05 and 12'); process.exit(2) }
  mkdirSync(dir, { recursive: true })
  for (const s of list) {
    const png = makePng(s * 1048576)
    const file = join(dir, `soak-img-${s}mib.png`)
    writeFileSync(file, png)
    console.log(`${file}  ${png.length} bytes${png.length > 10 * 1048576 ? '  (OVER the 10 MiB cap on purpose: expect skipped=media_too_large / 413)' : ''}`)
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main()
