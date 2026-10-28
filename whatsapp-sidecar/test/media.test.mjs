import test from 'node:test'
import assert from 'node:assert/strict'
import { Readable } from 'node:stream'
import { Gate, MAX_IMAGE_BYTES, MediaStore, MediaTimeout, MediaTooLarge, declaredLength, downloadCapped, readCapped, sha256Hex, sniffImage } from '../src/media.mjs'
import { loadConfig } from '../src/config.mjs'
import { ImgBuf } from './image-helpers.mjs'
import { MEDIA_HOST, downloadMedia } from '../src/wa.mjs'

const MB = 1048576
const clock = () => { const c = { now: 1_000_000, fn: () => c.now }; return c }

test('readCapped counts received bytes and stops the stream at the cap', async () => {
  const ok = await readCapped(Readable.from([Buffer.alloc(10), Buffer.alloc(5)]), 15)
  assert.equal(ok.length, 15)
  let produced = 0
  const endless = Readable.from((function* () { for (;;) { produced++; yield Buffer.alloc(1024) } })())
  await assert.rejects(readCapped(endless, 10 * 1024), MediaTooLarge)
  assert.ok(produced < 100, 'must stop pulling after the cap')
  assert.ok(endless.destroyed)
})

test('declaredLength fails closed on junk and understands Long-like values', () => {
  assert.equal(declaredLength(undefined), Infinity)
  assert.equal(declaredLength('abc'), Infinity)
  assert.equal(declaredLength(-1), Infinity)
  assert.equal(declaredLength({ toNumber: () => 1234 }), 1234)
  assert.equal(declaredLength(77), 77)
})

test('downloadCapped: timeout destroys a stalled stream; a hanging open() also times out; late stream is destroyed', async () => {
  const stalled = new Readable({ read() {} })
  await assert.rejects(downloadCapped(() => stalled, 100, 30), MediaTimeout)
  assert.ok(stalled.destroyed)
  let late
  await assert.rejects(downloadCapped(() => new Promise((r) => setTimeout(() => r((late = new Readable({ read() {} }))), 60)), 100, 20), MediaTimeout)
  await new Promise((r) => setTimeout(r, 90))
  assert.ok(late.destroyed)
  await assert.rejects(downloadCapped(() => { throw new Error('boom') }, 100, 50), /boom/)
})

test('Gate never runs more than n at once and drains the queue', async () => {
  const g = new Gate(2); let active = 0, peak = 0, done = 0
  const job = () => g.run(async () => { active++; peak = Math.max(peak, active); await new Promise((r) => setTimeout(r, 5)); active--; done++ })
  await Promise.all(Array.from({ length: 12 }, job))
  assert.equal(peak, 2); assert.equal(done, 12); assert.equal(g.active, 0)
  await assert.rejects(g.run(async () => { throw new Error('x') }), /x/)
  assert.equal(g.active, 0)
})

test('sniffImage recognises the four types and rejects everything else', () => {
  for (const t of ['image/jpeg', 'image/png', 'image/webp', 'image/gif']) assert.equal(sniffImage(ImgBuf(t)), t)
  assert.equal(sniffImage(Buffer.from('<html><script>alert(1)</script></html>')), null)
  assert.equal(sniffImage(Buffer.alloc(3)), null)
  assert.equal(sniffImage(null), null)
})

// ---- MediaStore ----
test('store: round trip, ids are random 128-bit hex and unrelated to input', () => {
  const st = new MediaStore({ maxBytes: 32 * MB })
  const a = st.put('s1', 'image/png', Buffer.from('aaa')), b = st.put('s1', 'image/png', Buffer.from('aaa'))
  assert.match(a, /^[0-9a-f]{32}$/); assert.notEqual(a, b)
  const got = st.get('s1', a)
  assert.equal(got.mime, 'image/png'); assert.equal(got.buf.toString(), 'aaa')
})

test('store: TTL is 10 minutes by default; expired entries 404 and free their bytes', () => {
  const c = clock(); const st = new MediaStore({ maxBytes: 32 * MB, now: c.fn })
  const id = st.put('s1', 'image/png', Buffer.alloc(1000))
  c.now += 10 * 60000 - 1
  assert.ok(st.get('s1', id))
  c.now += 1
  assert.equal(st.get('s1', id), null)
  assert.equal(st.size, 0); assert.equal(st.total, 0)
  const id2 = st.put('s1', 'image/png', Buffer.alloc(1000))
  c.now += 10 * 60000
  st.sweep()                                          // the periodic sweep frees bytes nobody asks for
  assert.equal(st.size, 0); assert.equal(st.get('s1', id2), null)
})

test('store: at most 8 items per session, oldest evicted first, other sessions untouched', () => {
  const c = clock(); const st = new MediaStore({ maxBytes: 32 * MB, now: c.fn })
  const other = st.put('s2', 'image/png', Buffer.alloc(10))
  const ids = []
  for (let i = 0; i < 10; i++) { c.now += 1; ids.push(st.put('s1', 'image/png', Buffer.alloc(10))) }
  assert.equal(ids.slice(0, 2).every((id) => st.get('s1', id) === null), true)
  assert.equal(ids.slice(2).every((id) => st.get('s1', id) !== null), true)
  assert.ok(st.get('s2', other))
  assert.equal(st.size, 9)
})

test('store: total byte cap evicts globally oldest first; an item larger than the cap is refused', () => {
  const c = clock(); const st = new MediaStore({ maxBytes: 25 * MB, now: c.fn })
  const a = st.put('s1', 'image/png', Buffer.alloc(10 * MB)); c.now++
  const b = st.put('s2', 'image/png', Buffer.alloc(10 * MB)); c.now++
  const d = st.put('s3', 'image/png', Buffer.alloc(10 * MB))
  assert.equal(st.get('s1', a), null)                 // oldest went first, across sessions
  assert.ok(st.get('s2', b)); assert.ok(st.get('s3', d))
  assert.equal(st.total, 20 * MB)
  assert.equal(st.put('s1', 'image/png', Buffer.alloc(26 * MB)), null)
})

test('store: another session key gets the same answer as an unknown id; bad ids are never looked up', () => {
  const st = new MediaStore({ maxBytes: 32 * MB })
  const id = st.put('tenant-a', 'image/png', Buffer.from('secret'))
  assert.equal(st.get('tenant-b', id), null)
  assert.ok(st.get('tenant-a', id))                   // and the owner is unaffected by the foreign probe
  for (const bad of ['', 'x', id.toUpperCase(), `${id}0`, '../../etc/passwd', undefined, null, 5, {}]) assert.equal(st.get('tenant-a', bad), null)
})

test('store: clearSession removes only that session', () => {
  const st = new MediaStore({ maxBytes: 32 * MB })
  const a = st.put('s1', 'image/png', Buffer.alloc(100)), b = st.put('s2', 'image/png', Buffer.alloc(100))
  st.clearSession('s1')
  assert.equal(st.get('s1', a), null); assert.ok(st.get('s2', b)); assert.equal(st.total, 100)
})

test('store stays under its cap with many large inputs (and byte accounting never drifts)', () => {
  const c = clock(); const st = new MediaStore({ maxBytes: 32 * MB, now: c.fn })
  let peak = 0
  for (let i = 0; i < 400; i++) {
    c.now += 1000
    st.put(`s${i % 5}`, 'image/jpeg', Buffer.allocUnsafe((1 + (i % 10)) * MB))
    peak = Math.max(peak, st.total)
    assert.ok(st.total <= 32 * MB, `total ${st.total}`)
    let sum = 0; for (const e of st.items.values()) sum += e.buf.length
    assert.equal(sum, st.total)
  }
  assert.ok(peak <= 32 * MB)
  for (let s = 0; s < 5; s++) assert.ok([...st.items.values()].filter((e) => e.session === `s${s}`).length <= 8)
})

test('sha256Hex is the plain digest of the bytes', () => {
  assert.equal(sha256Hex(Buffer.from('abc')), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
})

test('config: MEDIA_* defaults, parsing and validation', () => {
  const base = { WHATSAPP_SIDECAR_TOKEN: 't'.repeat(32), WHATSAPP_WEBHOOK_SECRET: 's'.repeat(32), WHATSAPP_AUTH_MASTER_KEY: 'ab'.repeat(32), BACKEND_BASE: 'http://api:8000', WHATSAPP_SIDECAR_DATABASE_URL: 'x', WHATSAPP_SIDECAR_REDIS_URL: 'y' }
  const d = loadConfig(base)
  assert.equal(d.mediaStoreMaxMb, 32); assert.deepEqual(d.mediaUrlAllowlist, ['res.cloudinary.com'])
  const c = loadConfig({ ...base, MEDIA_STORE_MAX_MB: '16', MEDIA_URL_ALLOWLIST: ' Res.Cloudinary.com, cdn.example.org ' })
  assert.equal(c.mediaStoreMaxMb, 16); assert.deepEqual(c.mediaUrlAllowlist, ['res.cloudinary.com', 'cdn.example.org'])
  for (const bad of ['*.example.com', '10.0.0.1', 'http://x.com', 'a.com:8080', 'localhost', ',']) assert.throws(() => loadConfig({ ...base, MEDIA_URL_ALLOWLIST: bad }), /MEDIA_URL_ALLOWLIST/)
  assert.throws(() => loadConfig({ ...base, MEDIA_STORE_MAX_MB: '5' }), /MEDIA_STORE_MAX_MB/)   // below one maximum-size image
  assert.equal(MAX_IMAGE_BYTES, 10 * MB)
})

test('store: a single session may hold at most half the store in bytes, so it cannot flush the others', () => {
  const c = clock(); const st = new MediaStore({ maxBytes: 32 * MB, now: c.fn })
  const other = st.put('s2', 'image/png', Buffer.alloc(10 * MB)); c.now++
  for (let i = 0; i < 6; i++) { c.now++; st.put('s1', 'image/png', Buffer.alloc(9 * MB)) }
  assert.ok(st.get('s2', other))
  assert.ok([...st.items.values()].filter((e) => e.session === 's1').reduce((n, e) => n + e.buf.length, 0) <= 16 * MB)
})

test('wa.downloadMedia: the request goes to the fixed WhatsApp media host, never to the sender-supplied url', async () => {
  const urls = []
  const realFetch = globalThis.fetch
  globalThis.fetch = async (u) => { urls.push(String(u)); return new Response('nope', { status: 403 }) }
  try {
    const msg = { key: { id: 'X', remoteJid: 'x@s.whatsapp.net' }, message: { imageMessage: { url: 'http://127.0.0.1:9/evil', directPath: '/o1/v/t62.x/abc?ccb=1', mediaKey: Buffer.alloc(32, 1), mimetype: 'image/png' } } }
    await assert.rejects(downloadMedia(msg, { updateMediaMessage: async () => { throw new Error('no reupload') } }))
    // a message that still carries the sender's url as the ONLY locator must not be fetched from there either
    const m2 = { key: msg.key, message: { imageMessage: { url: '', directPath: '/o1/v/t62.x/def', mediaKey: Buffer.alloc(32, 1), mimetype: 'image/png' } } }
    await assert.rejects(downloadMedia(m2, { updateMediaMessage: async () => { throw new Error('no reupload') } }))
  } finally { globalThis.fetch = realFetch }
  assert.ok(urls.length >= 2)
  for (const u of urls) assert.ok(u.startsWith(`https://${MEDIA_HOST}/`), u)
})
