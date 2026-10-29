// HTTP surface for images: GET /sessions/{key}/media/{id} and POST /sessions/{key}/send with an image.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createApi } from '../src/api.mjs'
import { ApiError } from '../src/session.mjs'
import { ImgBuf } from './image-helpers.mjs'
import { TOKEN, makeHarness, pairAndConnect, textMsg, until } from './helpers.mjs'

const CUST = '2348012345678@s.whatsapp.net'
const URL1 = 'https://res.cloudinary.com/demo/image/upload/v1/pic.png'

async function boot(over) {
  const h = makeHarness(over)
  const server = createApi({ manager: h.mgr, token: TOKEN, log: h.logCap.log })
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const base = `http://127.0.0.1:${server.address().port}`
  const raw = (method, path, body, token = TOKEN) => fetch(base + path, { method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
  const call = async (method, path, body, token) => { const r = await raw(method, path, body, token); return { status: r.status, body: await r.json() } }
  return { h, raw, call, close: () => new Promise((r) => { server.close(r); server.closeAllConnections() }) }
}

async function withImage(b, key = 'tenant-a', phone = '2348000000001', bytes = ImgBuf('image/jpeg', 2048, 3)) {
  await pairAndConnect(b.h, key, phone)
  b.h.mediaFor[`I-${key}`] = bytes
  b.h.mgr.get(key).sock.upsert([{ key: { id: `I-${key}`, remoteJid: CUST, fromMe: false }, message: { imageMessage: { mimetype: 'image/jpeg', caption: 'c', directPath: '/o1/v/t62.x/abc?ccb=1', mediaKey: Buffer.alloc(32, 5) } }, pushName: 'Ada', messageTimestamp: 1_800_000_000 }])
  await until(() => b.h.events('message.inbound').some((e) => e.session_key === key))
  return b.h.events('message.inbound').find((e) => e.session_key === key).media
}

test('GET media: 200 with the raw bytes, the stored content-type and hardening headers', async () => {
  const b = await boot(); const bytes = ImgBuf('image/jpeg', 2048, 3)
  const media = await withImage(b, 'tenant-a', '2348000000001', bytes)
  const r = await b.raw('GET', `/sessions/tenant-a/media/${media.id}`)
  assert.equal(r.status, 200)
  assert.equal(r.headers.get('content-type'), 'image/jpeg'); assert.equal(r.headers.get('content-length'), '2048')
  assert.equal(r.headers.get('cache-control'), 'no-store'); assert.equal(r.headers.get('x-content-type-options'), 'nosniff')
  assert.ok(Buffer.from(await r.arrayBuffer()).equals(bytes))
  assert.equal((await b.raw('GET', `/sessions/tenant-a/media/${media.id}`)).status, 200)    // reading does not consume it (TTL only)
  await b.close()
})

test('GET media: 404 for unknown id, malformed id, unknown session, expired id, and ANOTHER session\'s id (never 403, identical bodies)', async () => {
  const b = await boot()
  const media = await withImage(b, 'tenant-a', '2348000000001'); await pairAndConnect(b.h, 'tenant-b', '2348000000002')
  const notFound = { status: 404, body: { error: 'not_found' } }
  assert.deepEqual(await b.call('GET', `/sessions/tenant-b/media/${media.id}`), notFound)                       // cross-tenant
  assert.deepEqual(await b.call('GET', `/sessions/tenant-b/media/${'0'.repeat(32)}`), notFound)                 // unknown: same answer
  assert.deepEqual(await b.call('GET', '/sessions/tenant-a/media/nope'), notFound)
  assert.deepEqual(await b.call('GET', `/sessions/tenant-a/media/${media.id.toUpperCase()}`), notFound)
  assert.deepEqual(await b.call('GET', `/sessions/ghost/media/${media.id}`), notFound)
  assert.deepEqual(await b.call('GET', `/sessions/bad key/media/${media.id}`), notFound)
  assert.equal((await b.raw('GET', `/sessions/tenant-a/media/${media.id}`)).status, 200)                       // the owner is unaffected by the probes
  b.h.clock.now += 10 * 60000
  assert.deepEqual(await b.call('GET', `/sessions/tenant-a/media/${media.id}`), notFound)                       // TTL
  await b.close()
})

test('GET media requires the bearer token; other methods are 405; a logged-out session 404s', async () => {
  const b = await boot(); const media = await withImage(b)
  const p = `/sessions/tenant-a/media/${media.id}`
  for (const t of [null, 'wrong']) assert.deepEqual(await b.call('GET', p, undefined, t), { status: 401, body: { error: 'unauthorized' } })
  assert.equal((await b.call('POST', p, {})).status, 405); assert.equal((await b.call('DELETE', p)).status, 405)
  assert.equal((await b.call('DELETE', '/sessions/tenant-a')).status, 200)
  assert.equal((await b.call('GET', p)).status, 404)
  await b.close()
})

async function connectedApi(over) {
  const b = await boot(over)
  await pairAndConnect(b.h)
  b.h.last().upsert([textMsg('IN1', CUST, 'hi')]); await until(() => b.h.events('message.inbound').length === 1)
  b.h.images[URL1] = { buf: ImgBuf('image/png', 300), mime: 'image/png' }
  return b
}

test('POST send with an image: 200 { message_id }; caption passed through; text-only sends unchanged', async () => {
  const b = await connectedApi()
  const r = await b.call('POST', '/sessions/tenant-a/send', { to_ref: CUST, image: { url: URL1, caption: 'Look' } })
  assert.equal(r.status, 200); assert.deepEqual(Object.keys(r.body), ['message_id'])
  assert.equal(b.h.last().calls.find((c) => c[0] === 'send')[4].caption, 'Look')
  const t = await b.call('POST', '/sessions/tenant-a/send', { to_ref: CUST, text: 'plain' })
  assert.equal(t.status, 200); assert.equal(b.h.last().calls.filter((c) => c[0] === 'send').at(-1)[2], 'plain')
  assert.equal((await b.call('POST', '/sessions/tenant-a/send', { to_ref: CUST, image: { url: URL1 } })).status, 200)   // caption optional
  await b.close()
})

test('POST send validation: strict shapes, one of text/image, 400 invalid_image_url for bad URLs; nothing is fetched or sent', async () => {
  const b = await connectedApi()
  const send = (body) => b.call('POST', '/sessions/tenant-a/send', body)
  const badReq = (r, detail) => { assert.equal(r.status, 400); assert.equal(r.body.error, 'bad_request'); if (detail) assert.equal(r.body.detail, detail) }
  badReq(await send({ to_ref: CUST }), 'to_ref/text')
  badReq(await send({ to_ref: CUST, text: '   ' }), 'to_ref/text')
  badReq(await send({ to_ref: CUST, text: 'hi', image: { url: URL1 } }), 'text_and_image')
  badReq(await send({ to_ref: CUST, image: null }), 'image')
  badReq(await send({ to_ref: CUST, image: 'https://res.cloudinary.com/a.png' }), 'image')
  badReq(await send({ to_ref: CUST, image: [URL1] }), 'image')
  badReq(await send({ to_ref: CUST, image: { url: URL1, headers: { x: 'y' } } }), 'image')
  badReq(await send({ to_ref: CUST, image: { url: URL1, caption: 5 } }), 'image.caption')
  badReq(await send({ to_ref: CUST, image: { url: URL1, caption: 'x'.repeat(5000) } }), 'image.caption')
  badReq(await send({ image: { url: URL1 } }), 'to_ref')
  for (const url of [undefined, null, 5, {}, '', 'http://res.cloudinary.com/a.png', 'https://evil.com/a.png', 'https://res.cloudinary.com@evil.com/a.png', 'https://127.0.0.1/a.png', 'https://u:p@res.cloudinary.com/a.png', 'file:///etc/passwd']) {
    assert.deepEqual(await send({ to_ref: CUST, image: { url } }), { status: 400, body: { error: 'invalid_image_url' } }, String(url))
  }
  assert.deepEqual(b.h.fetches, []); assert.equal(b.h.last().calls.filter((c) => c[0] === 'send').length, 0)
  await b.close()
})

test('POST send image: contract error codes end to end (413, 415, 400, 409, 429, 503; 502 for an unreachable host)', async () => {
  const b = await connectedApi({ cfg: { sendPerMin: 4 } })
  const send = (url) => b.call('POST', '/sessions/tenant-a/send', { to_ref: CUST, image: { url } })
  const u = (n) => `https://res.cloudinary.com/${n}.png`
  b.h.images[u('big')] = new ApiError(413, 'image_too_large'); b.h.images[u('html')] = new ApiError(415, 'unsupported_media_type'); b.h.images[u('timeout')] = new ApiError(502, 'image_fetch_failed')
  assert.deepEqual(await send(u('big')), { status: 413, body: { error: 'image_too_large' } })
  assert.deepEqual(await send(u('html')), { status: 415, body: { error: 'unsupported_media_type' } })
  assert.deepEqual(await send(u('missing')), { status: 400, body: { error: 'invalid_image_url' } })
  assert.deepEqual(await send(u('timeout')), { status: 502, body: { error: 'image_fetch_failed' } })
  assert.deepEqual(await b.call('POST', '/sessions/tenant-a/send', { to_ref: '2347000000000@s.whatsapp.net', image: { url: URL1 } }), { status: 409, body: { error: 'reply_only' } })
  assert.deepEqual(await send(URL1), { status: 429, body: { error: 'rate_cap' } })       // 4 tokens went on the four fetch attempts above; the 5th is refused
  b.h.last().close(428); await until(() => b.h.mgr.get('tenant-a').status === 'reconnecting')
  assert.deepEqual(await send(URL1), { status: 503, body: { error: 'not_connected' } })
  await b.close()
})

test('POST send image body is bounded (64 KB), and the HTTP layer logs no url, caption or bytes', async () => {
  const b = await connectedApi()
  const r = await b.call('POST', '/sessions/tenant-a/send', { to_ref: CUST, image: { url: URL1, caption: 'x'.repeat(70000) } }).catch(() => ({ status: 'reset' }))
  assert.ok(r.status === 413 || r.status === 'reset')     // the server may cut the connection instead of answering
  assert.deepEqual(b.h.fetches, [])
  await b.call('POST', '/sessions/tenant-a/send', { to_ref: CUST, image: { url: URL1, caption: 'secret-caption-text' } })
  assert.ok(!b.h.logCap.text().includes('secret-caption-text')); assert.ok(!b.h.logCap.text().includes('pic.png'))
  await b.close()
})
