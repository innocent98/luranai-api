// Synthetic soak (fake socket, one process) and the test-image generator. Proves the tooling end to end; says nothing about real RAM.
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { inflateSync, crc32 } from 'node:zlib'
import { MAX_IMAGE_BYTES, sniffImage } from '../src/media.mjs'
import { renderReport } from '../scripts/soak-lib.mjs'
import { makePng } from '../scripts/soak-make-images.mjs'
import { runSynthetic } from '../scripts/soak-synthetic.mjs'

test('synthetic run walks every step, is labelled synthetic and produces no sizing', async () => {
  const out = join(mkdtempSync(join(tmpdir(), 'soak-syn-')), 'syn.csv')
  const report = await runSynthetic({ sessions: 2, stepSeconds: 0.4, intervalSeconds: 0.08, out })
  assert.equal(report.synthetic, true); assert.deepEqual(report.sources, ['synthetic'])
  assert.deepEqual(report.steady.map((s) => s.sessions), [0, 1, 2])
  assert.equal(report.bursts[0].step, 'image-burst'); assert.ok(report.bursts[0].peak_rss > 0)
  assert.ok(report.hold && report.hold.samples >= 2)
  assert.equal(report.storm.expected, 2); assert.equal(report.storm.restored, true)
  assert.equal(report.recommendation.valid, false); assert.match(report.recommendation.reason, /synthetic/)
  const text = renderReport(report)
  assert.match(text, /NOT representative of real Baileys sessions/); assert.match(text, /NONE: synthetic data/)
  const csv = readFileSync(out, 'utf8')
  assert.ok(csv.split('\n')[0].startsWith('iso_time,elapsed_s,source,step,'))
  assert.ok(csv.split('\n').filter(Boolean).slice(1).every((l) => l.split(',')[2] === 'synthetic'))
})

test('image generator: valid PNG at the requested size, accepted by the sidecar magic-byte check, 9.5 MiB stays under the 10 MiB cap', () => {
  for (const mib of [0.1, 1, 9.5]) {
    const png = makePng(mib * 1048576)
    assert.ok(Math.abs(png.length - mib * 1048576) < 8192, `${mib} MiB -> ${png.length}`)
    assert.equal(sniffImage(png), 'image/png')
    // walk the chunks: lengths and CRCs are consistent, IHDR dimensions match the inflated pixel data
    let off = 8, w, h, idat = Buffer.alloc(0), types = []
    while (off < png.length) {
      const len = png.readUInt32BE(off), type = png.toString('latin1', off + 4, off + 8), data = png.subarray(off + 8, off + 8 + len)
      assert.equal(png.readUInt32BE(off + 8 + len), crc32(Buffer.concat([Buffer.from(type, 'latin1'), data])), `crc ${type}`)
      if (type === 'IHDR') { w = data.readUInt32BE(0); h = data.readUInt32BE(4) }
      if (type === 'IDAT') idat = Buffer.concat([idat, data])
      types.push(type); off += 12 + len
    }
    assert.deepEqual(types, ['IHDR', 'IDAT', 'IEND'])
    assert.equal(inflateSync(idat).length, h * (w * 3 + 1))
  }
  assert.ok(makePng(9.5 * 1048576).length < MAX_IMAGE_BYTES)
  assert.ok(makePng(10.5 * 1048576).length > MAX_IMAGE_BYTES)
  assert.ok(makePng(1048576).equals(makePng(1048576)), 'deterministic')
})
