// Inbound image capture + outbound image sends, against the fake-socket harness (no network, no Baileys).
import test from 'node:test'
import assert from 'node:assert/strict'
import { Readable } from 'node:stream'
import { runInNewContext } from 'node:vm'
import v8 from 'node:v8'
import { sha256Hex } from '../src/media.mjs'
import { ApiError } from '../src/session.mjs'
import { ImgBuf } from './image-helpers.mjs'
import { makeHarness, pairAndConnect, textMsg, tick, until } from './helpers.mjs'

const CUST = '2348012345678@s.whatsapp.net'
const MB = 1048576
const imageMsg = (id, jid, { caption, mime = 'image/png', len = 1000, extra = {} } = {}) => ({
  key: { id, remoteJid: jid, fromMe: false },
  message: { imageMessage: { mimetype: mime, fileLength: len, url: 'https://mmg.whatsapp.net/o1/v/t62.x/abc?ccb=1', directPath: '/o1/v/t62.x/abc?ccb=1&oh=00', mediaKey: Buffer.alloc(32, 5), ...(caption !== undefined ? { caption } : {}) } },
  pushName: 'Ada', messageTimestamp: 1_800_000_000, ...extra,
})
const inbound = (h) => h.events('message.inbound')
const URL1 = 'https://res.cloudinary.com/demo/image/upload/v1/pic.png'

// ---------- inbound ----------
test('inbound image: downloaded, stored, webhook has kind image + caption + media{id,mime,bytes,sha256}; bytes retrievable and hash matches', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const bytes = ImgBuf('image/jpeg', 5000, 9)
  h.mediaFor.IMG1 = bytes
  h.last().upsert([imageMsg('IMG1', CUST, { caption: 'my receipt', len: 5000, mime: 'image/jpeg' })])
  await until(() => inbound(h).length === 1)
  const ev = inbound(h)[0]
  assert.deepEqual(Object.keys(ev), ['session_key', 'type', 'message_id', 'from_ref', 'from_phone_digits', 'push_name', 'kind', 'text', 'media'])
  assert.equal(ev.kind, 'image'); assert.equal(ev.text, 'my receipt'); assert.equal(ev.message_id, 'IMG1')
  assert.deepEqual(Object.keys(ev.media), ['id', 'mime', 'bytes', 'sha256'])
  assert.equal(ev.media.mime, 'image/jpeg'); assert.equal(ev.media.bytes, 5000); assert.equal(ev.media.sha256, sha256Hex(bytes))
  assert.match(ev.media.id, /^[0-9a-f]{32}$/); assert.ok(!ev.media.id.includes('IMG1'))
  const got = h.mgr.media('tenant-a', ev.media.id)
  assert.ok(got.buf.equals(bytes)); assert.equal(got.mime, 'image/jpeg')
  assert.equal(sha256Hex(got.buf), ev.media.sha256)
})

test('inbound image without a caption: text is the empty string; png, webp and gif are images too', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const kinds = [['A', 'image/png'], ['B', 'image/webp'], ['C', 'image/gif'], ['D', 'IMAGE/JPEG']]
  for (const [id, mime] of kinds) h.mediaFor[id] = ImgBuf(mime === 'IMAGE/JPEG' ? 'image/jpeg' : mime, 100)
  h.last().upsert(kinds.map(([id, mime]) => imageMsg(id, CUST, { mime })))
  await until(() => inbound(h).length === 4)
  assert.deepEqual(inbound(h).map((e) => [e.kind, e.text, e.media.mime]), [['image', '', 'image/png'], ['image', '', 'image/webp'], ['image', '', 'image/gif'], ['image', '', 'image/jpeg']])
})

test('inbound image over the cap with a LYING declared length: counted on received bytes, skipped media_too_large, nothing stored, stream cut early', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  let produced = 0
  const body = Readable.from((function* () { for (;;) { produced++; yield Buffer.alloc(MB) } })())
  h.mediaFor.BIG = body
  h.last().upsert([imageMsg('BIG', CUST, { caption: 'huge', len: 1234 })])    // claims 1.2 KB
  await until(() => inbound(h).length === 1)
  const ev = inbound(h)[0]
  assert.equal(ev.kind, 'other'); assert.equal(ev.text, ''); assert.equal(ev.skipped, 'media_too_large'); assert.ok(!('media' in ev))
  assert.ok(produced <= 12, `read ${produced} MB before stopping`); assert.ok(body.destroyed)
  assert.equal(h.deps.media.size, 0)
})

test('inbound image: exactly 10 MB is kept, one byte more is skipped; a declared size over the cap is skipped without downloading', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.mediaFor.EDGE = ImgBuf('image/png', 10 * MB)
  h.mediaFor.OVER = ImgBuf('image/png', 10 * MB + 1)
  h.last().upsert([imageMsg('EDGE', CUST), imageMsg('OVER', CUST), imageMsg('DECL', CUST, { len: 50 * MB }), imageMsg('LONG', CUST, { len: { toNumber: () => 11 * MB } })])
  await until(() => inbound(h).length === 4)
  assert.deepEqual(inbound(h).map((e) => [e.message_id, e.kind, e.skipped]), [['EDGE', 'image', undefined], ['OVER', 'other', 'media_too_large'], ['DECL', 'other', 'media_too_large'], ['LONG', 'other', 'media_too_large']])
  assert.deepEqual(h.downloads, ['EDGE', 'OVER'])
})

test('non-image media (video, audio, document, sticker, heic/svg "images") stays kind other, empty text, and is NOT downloaded', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const msg = (id, message) => ({ key: { id, remoteJid: CUST, fromMe: false }, message, pushName: 'Ada', messageTimestamp: 1_800_000_000 })
  h.last().upsert([
    msg('V', { videoMessage: { mimetype: 'video/mp4', caption: 'clip' } }),
    msg('AU', { audioMessage: { mimetype: 'audio/ogg; codecs=opus', ptt: true } }),
    msg('DOC', { documentMessage: { mimetype: 'application/pdf', fileName: 'x.pdf', caption: 'invoice' } }),
    msg('ST', { stickerMessage: { mimetype: 'image/webp' } }),
    msg('HEIC', { imageMessage: { mimetype: 'image/heic', caption: 'c' } }),
    msg('SVG', { imageMessage: { mimetype: 'image/svg+xml' } }),
    msg('NOMIME', { imageMessage: { caption: 'c' } }),
  ])
  await until(() => inbound(h).length === 7)
  for (const e of inbound(h)) { assert.equal(e.kind, 'other', e.message_id); assert.equal(e.text, ''); assert.ok(!('media' in e)); assert.ok(!('skipped' in e)) }
  assert.deepEqual(h.downloads, [])
  assert.equal(h.deps.media.size, 0)
})

test('download failure or stall: kind other + skipped media_unavailable (the customer message is not dropped); later messages still flow in order', async () => {
  const h = makeHarness({ cfg: { mediaDownloadTimeoutMs: 40 } }); await pairAndConnect(h)
  h.mediaFor.F1 = new Error('expired media')
  h.mediaFor.F2 = new Readable({ read() {} })                // never delivers
  h.last().upsert([imageMsg('F1', CUST, { caption: 'x' }), imageMsg('F2', CUST), textMsg('T1', CUST, 'after')])
  await until(() => inbound(h).length === 3)
  assert.deepEqual(inbound(h).map((e) => [e.message_id, e.kind, e.skipped]), [['F1', 'other', 'media_unavailable'], ['F2', 'other', 'media_unavailable'], ['T1', 'text', undefined]])
  assert.ok(h.mediaFor.F2.destroyed)
})

test('a message that is a duplicate is not downloaded twice; history (append) is never downloaded', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.mediaFor.D1 = ImgBuf('image/png', 100); h.mediaFor.H1 = ImgBuf('image/png', 100)
  h.last().upsert([imageMsg('D1', CUST), imageMsg('D1', CUST)])
  h.last().upsert([imageMsg('H1', CUST)], 'append')
  await tick(40)
  assert.equal(inbound(h).length, 1); assert.deepEqual(h.downloads, ['D1'])
})

test('owner-sent (fromMe) images are never downloaded and emit no caption text', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.last().upsert([{ ...imageMsg('OWN1', CUST, { caption: 'secret caption' }), key: { id: 'OWN1', remoteJid: CUST, fromMe: true } }])
  await until(() => h.events('message.outbound_external').length === 1)
  assert.equal(h.events('message.outbound_external')[0].text, ''); assert.deepEqual(h.downloads, [])
})

test('per-session item cap through the real path: the 9th image evicts the 1st; other sessions are untouched', async () => {
  const h = makeHarness(); await pairAndConnect(h, 'tenant-a', '2348000000001'); await pairAndConnect(h, 'tenant-b', '2348000000002')
  const sa = h.mgr.get('tenant-a').sock, sb = h.mgr.get('tenant-b').sock
  h.mediaFor.B1 = ImgBuf('image/png', 100); sb.upsert([imageMsg('B1', CUST)])
  for (let i = 1; i <= 9; i++) { h.mediaFor[`A${i}`] = ImgBuf('image/png', 100, i); sa.upsert([imageMsg(`A${i}`, CUST)]) }
  await until(() => inbound(h).length === 10)
  const a = inbound(h).filter((e) => e.session_key === 'tenant-a'), b = inbound(h).find((e) => e.session_key === 'tenant-b')
  assert.equal(h.mgr.media('tenant-a', a[0].media.id), null)
  for (const e of a.slice(1)) assert.ok(h.mgr.media('tenant-a', e.media.id))
  assert.ok(h.mgr.media('tenant-b', b.media.id))
})

test('tenant isolation: one session cannot read another session\'s media id', async () => {
  const h = makeHarness(); await pairAndConnect(h, 'tenant-a', '2348000000001'); await pairAndConnect(h, 'tenant-b', '2348000000002')
  h.mediaFor.X = ImgBuf('image/png', 100)
  h.mgr.get('tenant-a').sock.upsert([imageMsg('X', CUST)])
  await until(() => inbound(h).length === 1)
  const id = inbound(h)[0].media.id
  assert.ok(h.mgr.media('tenant-a', id)); assert.equal(h.mgr.media('tenant-b', id), null); assert.equal(h.mgr.media('missing', id), null)
})

test('media is cleared when the session logs out (DELETE) and when WhatsApp unlinks it (401)', async () => {
  const h = makeHarness(); await pairAndConnect(h, 'tenant-a', '2348000000001'); await pairAndConnect(h, 'tenant-b', '2348000000002')
  h.mediaFor.A = ImgBuf('image/png', 100); h.mediaFor.B = ImgBuf('image/png', 100)
  h.mgr.get('tenant-a').sock.upsert([imageMsg('A', CUST)]); h.mgr.get('tenant-b').sock.upsert([imageMsg('B', CUST)])
  await until(() => inbound(h).length === 2)
  const idA = inbound(h).find((e) => e.session_key === 'tenant-a').media.id, idB = inbound(h).find((e) => e.session_key === 'tenant-b').media.id
  await h.mgr.remove('tenant-a')
  assert.equal(h.mgr.media('tenant-a', idA), null); assert.equal(h.deps.media.size, 1)
  h.mgr.get('tenant-b').sock.close(401)
  await until(() => h.mgr.get('tenant-b').status === 'logged_out')
  assert.equal(h.mgr.media('tenant-b', idB), null); assert.equal(h.deps.media.size, 0)
})

test('socket replaced (plain reconnect) while an image is downloading: reported as media_unavailable, not silently lost; nothing stored', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  let release; const gate = new Promise((r) => { release = r })
  h.deps.downloadMedia = async () => { await gate; return Readable.from([ImgBuf('image/png', 100)]) }
  h.last().upsert([imageMsg('SLOW', CUST)])
  await tick(20)
  h.last().close(428)                                   // reconnect: the old epoch is now stale
  await tick(10); release(); await until(() => inbound(h).length === 1)
  assert.deepEqual([inbound(h)[0].kind, inbound(h)[0].skipped], ['other', 'media_unavailable']); assert.equal(h.deps.media.size, 0)
})

test('session unlinked while an image is downloading: nothing is emitted and nothing is stored', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  let release; const gate = new Promise((r) => { release = r })
  h.deps.downloadMedia = async () => { await gate; return Readable.from([ImgBuf('image/png', 100)]) }
  h.last().upsert([imageMsg('SLOW', CUST)])
  await tick(20)
  const out = h.mgr.remove('tenant-a'); await tick(20); release(); await out; await tick(60)
  assert.equal(inbound(h).length, 0); assert.equal(h.deps.media.size, 0)
})

test('SSRF: the sender-supplied url is never used; only a CDN-relative directPath + 32-byte key qualify, and Baileys sees a rebuilt message', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const mk = (id, img, extra = {}) => ({ key: { id, remoteJid: CUST, fromMe: false }, message: { imageMessage: { mimetype: 'image/png', ...img }, ...extra }, pushName: 'Ada', messageTimestamp: 1_800_000_000 })
  const key = Buffer.alloc(32, 1)
  h.mediaFor.OK1 = ImgBuf('image/png', 100)
  const seen = []
  const orig = h.deps.downloadMedia
  h.deps.downloadMedia = async (msg, sock) => { seen.push(msg); return orig(msg, sock) }
  h.last().upsert([
    mk('EV1', { url: 'http://127.0.0.1:9/x', mediaKey: key }),                                  // url only
    mk('EV2', { url: 'http://127.0.0.1:9/x', directPath: '@127.0.0.1:9/x', mediaKey: key }),      // userinfo trick
    mk('EV3', { directPath: '.evil.example/x', mediaKey: key }),                                  // host suffix trick
    mk('EV4', { directPath: 'http://evil.example/x', mediaKey: key }),
    mk('EV5', { directPath: '/ok path', mediaKey: key }),
    mk('EV6', { directPath: '/ok\\path', mediaKey: key }),
    mk('EV7', { directPath: '/o1/v/t62.x/abc' }),                                                 // no key
    mk('EV8', { directPath: '/o1/v/t62.x/abc', mediaKey: Buffer.alloc(5) }),
    mk('OK1', { url: 'http://127.0.0.1:9/x', directPath: '/o1/v/t62.x/abc?ccb=1', mediaKey: key, fileLength: 1, jpegThumbnail: Buffer.alloc(3) },
      { videoMessage: { url: 'http://127.0.0.1:9/video' }, documentMessage: { url: 'http://169.254.169.254/' } }),   // extra content types must not be what gets downloaded
  ])
  await until(() => inbound(h).length === 9)
  assert.deepEqual(inbound(h).slice(0, 8).map((e) => e.kind), Array(8).fill('other'))
  assert.equal(inbound(h)[8].kind, 'image')
  assert.deepEqual(h.downloads, ['OK1']); assert.equal(seen.length, 1)
  assert.deepEqual(Object.keys(seen[0].message), ['imageMessage'])
  assert.deepEqual(seen[0].message.imageMessage, { url: '', directPath: '/o1/v/t62.x/abc?ccb=1', mediaKey: key, mimetype: 'image/png' })
})

test('inbound bytes must match the declared image type (HTML/SVG/polyglots labelled image/jpeg are not served)', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.mediaFor.P1 = Buffer.from('<html><script>alert(1)</script></html> padding padding'); h.mediaFor.P2 = ImgBuf('image/png', 100)
  h.last().upsert([imageMsg('P1', CUST), imageMsg('P2', CUST, { mime: 'image/jpeg' })])
  await until(() => inbound(h).length === 2)
  assert.deepEqual(inbound(h).map((e) => [e.kind, e.skipped]), [['other', 'media_unavailable'], ['other', 'media_unavailable']])
  assert.equal(h.deps.media.size, 0)
})

test('one tenant flooding with images cannot evict another tenant\'s images (per-session byte quota)', async () => {
  const h = makeHarness(); await pairAndConnect(h, 'tenant-a', '2348000000001'); await pairAndConnect(h, 'tenant-b', '2348000000002')
  h.mediaFor.B1 = ImgBuf('image/png', 6 * MB); h.mgr.get('tenant-b').sock.upsert([imageMsg('B1', CUST)])
  await until(() => inbound(h).length === 1)
  const idB = inbound(h)[0].media.id
  for (let i = 0; i < 12; i++) { h.mediaFor[`F${i}`] = ImgBuf('image/png', 9 * MB, i); h.mgr.get('tenant-a').sock.upsert([imageMsg(`F${i}`, CUST)]) }
  await until(() => inbound(h).length === 13, 8000)
  assert.ok(h.mgr.media('tenant-b', idB), 'tenant B lost its image to tenant A\'s flood')
  assert.ok(h.deps.media.total <= 32 * MB)
})

test('a stalled image upload times out, frees the upload gate and the session keeps working', async () => {
  const h = await connected({ cfg: { mediaUploadTimeoutMs: 30, mediaUploadGraceMs: 10 } }); h.images[URL1] = png()
  const sock = h.last(), orig = sock.sendMessage.bind(sock)
  sock.sendMessage = (jid, content, o) => (content.image ? new Promise(() => {}) : orig(jid, content, o))
  const t0 = Date.now()
  await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: URL1 }), (e) => e.status === 502 && e.code === 'send_failed')
  assert.ok(Date.now() - t0 < 2000)
  assert.equal(h.deps.uploadGate.active, 0)
  assert.ok((await h.mgr.send('tenant-a', CUST, 'text still flows')).message_id)
})

test('logs and webhooks never carry image bytes, captions or phone numbers', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const marker = Buffer.from('UNIQUE-PIXEL-MARKER-0123456789')
  const bytes = Buffer.concat([ImgBuf('image/png', 12), marker])
  h.mediaFor.L1 = bytes; h.mediaFor.L2 = new Error('x')
  h.last().upsert([imageMsg('L1', CUST, { caption: 'caption-with-secret-words' }), imageMsg('L2', CUST, { caption: 'another-secret-caption' })])
  await until(() => inbound(h).length === 2)
  const logs = h.logCap.text()
  for (const s of ['caption-with-secret-words', 'another-secret-caption', '2348012345678', marker.toString(), marker.toString('base64'), marker.toString('hex'), bytes.toString('base64'), inbound(h)[0].media.id, inbound(h)[0].media.sha256]) {
    assert.ok(!logs.includes(s), `log leaked ${s.slice(0, 12)}`)
  }
  assert.ok(!JSON.stringify(inbound(h)).includes(marker.toString('base64')))   // the webhook carries a reference, never bytes
})

// ---------- memory discipline ----------
const forceGc = (() => { v8.setFlagsFromString('--expose-gc'); const gc = runInNewContext('gc'); return () => { gc(); gc() } })()

test('memory: repeated large inbound images stay bounded (store cap holds, buffers are released, RSS reaches a plateau instead of growing with traffic)', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const sock = h.last()
  const size = 6 * MB
  let n = 0, peakTotal = 0
  const push = async (count) => {
    for (let i = 0; i < count; i++, n++) {
      h.mediaFor[`M${n}`] = ImgBuf('image/png', size, n % 250)
      sock.upsert([imageMsg(`M${n}`, CUST)])
      await until(() => inbound(h).length === n + 1, 5000)
      delete h.mediaFor[`M${n}`]
      peakTotal = Math.max(peakTotal, h.deps.media.total)
      assert.ok(h.deps.media.total <= 32 * MB)
    }
  }
  forceGc()
  const start = process.memoryUsage()
  await push(25); forceGc()                              // warm-up: allocator and store reach steady state
  const plateau = process.memoryUsage()
  await push(80); forceGc()                              // a further 480 MB of traffic through a 32 MB store
  const end = process.memoryUsage()
  assert.ok(peakTotal <= 32 * MB)
  assert.ok(h.deps.media.size <= 5)                      // 32 MB / 6 MB
  // Buffer memory may hold the store (<= 32 MB) plus slack, never the traffic that passed through.
  assert.ok(end.arrayBuffers - start.arrayBuffers < 64 * MB, `arrayBuffers grew ${((end.arrayBuffers - start.arrayBuffers) / MB).toFixed(1)} MB`)
  assert.ok(end.rss - plateau.rss < 100 * MB, `rss kept growing after warm-up: +${((end.rss - plateau.rss) / MB).toFixed(1)} MB over 480 MB of traffic`)
  h.deps.media.clearSession('tenant-a'); forceGc()
  assert.ok(process.memoryUsage().arrayBuffers - start.arrayBuffers < 16 * MB)   // everything is released once the store is emptied
})

// ---------- outbound ----------
const connected = async (over) => { const h = makeHarness(over); await pairAndConnect(h); h.last().upsert([textMsg('IN1', CUST, 'hi')]); await until(() => inbound(h).length === 1); return h }
const png = (n = 300) => ({ buf: ImgBuf('image/png', n), mime: 'image/png' })

test('image send: fetches, humanises like a text send, sends a WhatsApp image with the caption; response is { message_id } only', async () => {
  const h = await connected()
  h.images[URL1] = png()
  const r = await h.mgr.send('tenant-a', CUST, '', { url: URL1, caption: 'Here is the diagram' })
  assert.deepEqual(Object.keys(r), ['message_id'])
  const s = h.last()
  assert.deepEqual(s.names().filter((n) => n !== 'pair'), ['read', 'sub', 'presence', 'presence', 'send'])
  const sent = s.calls.find((c) => c[0] === 'send')
  assert.equal(sent[1], CUST); assert.equal(sent[3], r.message_id)
  assert.ok(sent[4].image.equals(h.images[URL1].buf)); assert.equal(sent[4].mimetype, 'image/png'); assert.equal(sent[4].caption, 'Here is the diagram')
  assert.deepEqual(h.fetches, [URL1])
  assert.ok(h.mgr.get('tenant-a').sentIds.has(r.message_id))             // the echo must not look like the owner
  // the retry-receipt cache keeps the small proto, never the picture
  const kept = h.mgr.get('tenant-a').sentContent.get(r.message_id)
  assert.ok(kept && !Buffer.isBuffer(kept) && !('image' in kept))
  for (const v of h.mgr.get('tenant-a').sentContent.m.values()) assert.ok(!JSON.stringify(v).includes('"image":'))
})

test('image send without a caption sends no caption field; typing time is based on the caption length', async () => {
  const h = await connected()
  h.images[URL1] = png()
  await h.mgr.send('tenant-a', CUST, '', { url: URL1 })
  assert.ok(!('caption' in h.last().calls.find((c) => c[0] === 'send')[4]))
  const base = h.sleeps.at(-1)
  await h.mgr.send('tenant-a', CUST, '', { url: URL1, caption: 'x'.repeat(100) })
  assert.ok(h.sleeps.at(-1) > base)
})

test('image sends obey reply-only: cold chat, other number and expired window are refused BEFORE any fetch', async () => {
  const h = await connected()
  h.images[URL1] = png()
  await assert.rejects(h.mgr.send('tenant-a', '2347000000000@s.whatsapp.net', '', { url: URL1 }), (e) => e.status === 409 && e.code === 'reply_only')
  h.clock.now += 25 * 3600000
  await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: URL1 }), (e) => e.status === 409 && e.code === 'reply_only')
  assert.deepEqual(h.fetches, []); assert.equal(h.last().calls.filter((c) => c[0] === 'send').length, 0)
})

test('image sends obey not_connected and a bad to_ref', async () => {
  const h = await connected(); h.images[URL1] = png()
  await assert.rejects(h.mgr.send('tenant-a', 'x', '', { url: URL1 }), (e) => e.status === 400)
  h.last().close(428); await until(() => h.mgr.get('tenant-a').status === 'reconnecting')
  await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: URL1 }), (e) => e.status === 503 && e.code === 'not_connected')
  assert.deepEqual(h.fetches, [])
})

test('image sends share the token bucket and the daily cap with text; an image is exactly one send', async () => {
  const h = await connected({ cfg: { sendPerMin: 3 } }); h.images[URL1] = png()
  await h.mgr.send('tenant-a', CUST, 'text one')
  await h.mgr.send('tenant-a', CUST, '', { url: URL1 })
  await h.mgr.send('tenant-a', CUST, '', { url: URL1, caption: 'c' })
  await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: URL1 }), (e) => e.status === 429 && e.code === 'rate_cap')
  assert.equal(h.last().calls.filter((c) => c[0] === 'send').length, 3)
  assert.equal(h.fetches.length, 2)                      // the refused one never fetched

  const d = await connected({ cfg: { dailySendCap: 2 } }); d.images[URL1] = png()
  await d.mgr.send('tenant-a', CUST, '', { url: URL1 }); await d.mgr.send('tenant-a', CUST, 'text')
  await assert.rejects(d.mgr.send('tenant-a', CUST, '', { url: URL1 }), (e) => e.status === 429 && e.extra.scope === 'daily')
  assert.equal(d.fetches.length, 1)
})

test('fetch failures surface as their API errors, send nothing and do not touch presence', async () => {
  const h = await connected()
  for (const [u, status, code] of [['https://res.cloudinary.com/big.png', 413, 'image_too_large'], ['https://res.cloudinary.com/bad.bin', 415, 'unsupported_media_type'], ['https://res.cloudinary.com/gone.png', 400, 'invalid_image_url'], ['https://res.cloudinary.com/slow.png', 502, 'image_fetch_failed']]) {
    h.images[u] = new ApiError(status, code)
    await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: u }), (e) => e.status === status && e.code === code)
  }
  const s = h.last()
  assert.equal(s.calls.filter((c) => ['send', 'presence', 'read', 'sub'].includes(c[0])).length, 0)
})

test('a URL outside the allowlist or not https is refused with 400 invalid_image_url before anything is spent', async () => {
  const h = await connected({ cfg: { sendPerMin: 1 } })
  for (const u of ['http://res.cloudinary.com/a.png', 'https://evil.com/a.png', 'https://169.254.169.254/x.png', 'https://u:p@res.cloudinary.com/a.png']) {
    await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: u }), (e) => e.status === 400 && e.code === 'invalid_image_url')
  }
  assert.deepEqual(h.fetches, [])
  h.images[URL1] = png(); await h.mgr.send('tenant-a', CUST, '', { url: URL1 })    // the single token of the bucket is still there
})

test('a send that fails at WhatsApp is send_failed and the picture is not retained', async () => {
  const h = await connected(); h.images[URL1] = png()
  h.last().sendMessage = async () => { throw new Error('upload failed for 2348012345678') }
  await assert.rejects(h.mgr.send('tenant-a', CUST, '', { url: URL1 }), (e) => e.status === 502 && e.code === 'send_failed')
  assert.ok(!h.logCap.text().includes('2348012345678'))
  assert.equal(h.mgr.get('tenant-a').sentContent.size, 0)
})

test('image sends are serialised per session and the upload gate lets one upload through at a time, process-wide', async () => {
  const h = await connected(); h.images[URL1] = png()
  let active = 0, peak = 0
  const sock = h.last(), orig = sock.sendMessage.bind(sock)
  sock.sendMessage = async (...a) => { active++; peak = Math.max(peak, active); await tick(10); active--; return orig(...a) }
  await Promise.all([1, 2, 3].map((i) => h.mgr.send('tenant-a', CUST, '', { url: URL1, caption: String(i) })))
  assert.equal(peak, 1)
  assert.deepEqual(sock.calls.filter((c) => c[0] === 'send').map((c) => c[4].caption), ['1', '2', '3'])
})
