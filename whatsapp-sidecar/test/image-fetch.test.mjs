import test from 'node:test'
import assert from 'node:assert/strict'
import { Readable } from 'node:stream'
import { fetchImage, isBlockedIp, validateImageUrl } from '../src/image-fetch.mjs'
import { ImgBuf } from './image-helpers.mjs'

const ALLOW = ['res.cloudinary.com', 'cdn.example.org']
const OK = 'https://res.cloudinary.com/demo/image/upload/v1/a.png'
const code = (status, error) => (e) => e.status === status && e.code === error

test('validateImageUrl accepts a plain https URL on an allowlisted host (case-insensitive host, query ok)', () => {
  assert.equal(validateImageUrl(OK, ALLOW).hostname, 'res.cloudinary.com')
  assert.equal(validateImageUrl('https://RES.Cloudinary.com/a.jpg?x=1#f', ALLOW).hostname, 'res.cloudinary.com')
  assert.equal(validateImageUrl('https://res.cloudinary.com:443/a.jpg', ALLOW).hostname, 'res.cloudinary.com')   // default port normalises away
  // WHATWG parsing turns the backslash into a path separator: the host we validated is the host we connect to (we fetch the parsed URL)
  const tricky = validateImageUrl('https://res.cloudinary.com\\@evil.com/a.png', ALLOW)
  assert.equal(tricky.hostname, 'res.cloudinary.com'); assert.equal(tricky.pathname, '/@evil.com/a.png')
})

test('validateImageUrl rejects http, wrong host, credentials, odd ports, IP literals and junk (all 400 invalid_image_url)', () => {
  const bad = [
    'http://res.cloudinary.com/a.png',                       // not https
    'ftp://res.cloudinary.com/a.png', 'file:///etc/passwd', 'data:image/png;base64,AAAA', 'javascript:alert(1)',
    'https://evil.example.com/a.png',                         // not allowlisted
    'https://res.cloudinary.com.evil.com/a.png',              // suffix trick
    'https://evil.com/res.cloudinary.com/a.png',
    'https://sub.res.cloudinary.com/a.png',                   // no wildcard matching
    'https://res.cloudinary.com./a.png',                      // trailing dot
    'https://user:pw@res.cloudinary.com/a.png', 'https://user@res.cloudinary.com/a.png',
    'https://res.cloudinary.com:8443/a.png', 'https://res.cloudinary.com:80/a.png',
    'https://127.0.0.1/a.png', 'https://[::1]/a.png', 'https://169.254.169.254/latest/meta-data', 'https://2130706433/a.png', 'https://0x7f.1/a.png',
    'https://localhost/a.png', 'https://res.cloudinary.com/a b.png',
    '', ' ', 'not a url', 'https://', '//res.cloudinary.com/a.png', `https://res.cloudinary.com/${'a'.repeat(2100)}`,
    'https://res.cloudinary.com/a.png\nHost: evil.com',
    undefined, null, 5, {}, [],
  ]
  for (const u of bad) assert.throws(() => validateImageUrl(u, ALLOW), code(400, 'invalid_image_url'), `should reject ${String(u).slice(0, 60)}`)
  // an allowlist entry that is an IP literal still cannot be used
  assert.throws(() => validateImageUrl('https://10.0.0.5/a.png', ['10.0.0.5']), code(400, 'invalid_image_url'))
})

test('isBlockedIp: private, loopback, link-local, metadata, CGNAT, multicast and special-use ranges are blocked', () => {
  const blocked = [
    '0.0.0.0', '10.0.0.1', '10.255.255.255', '127.0.0.1', '127.8.8.8', '100.64.0.1', '100.127.255.255', '169.254.169.254', '169.254.0.1',
    '172.16.0.1', '172.31.255.255', '192.0.0.1', '192.0.2.5', '192.168.0.1', '192.168.255.255', '198.18.0.1', '198.19.255.255',
    '198.51.100.7', '203.0.113.9', '224.0.0.1', '239.255.255.250', '240.0.0.1', '255.255.255.255',
    '::', '::1', '::ffff:127.0.0.1', '::ffff:7f00:1', '::ffff:10.0.0.1', '::ffff:a9fe:a9fe', '::ffff:8.8.8.8',   // v4-mapped is blocked wholesale
    'fe80::1', 'fe80::1%eth0', 'fc00::1', 'fd12:3456::1', 'ff02::1', '64:ff9b::7f00:1', '64:ff9b::808:808', '2002:7f00:1::1', '2002:808:808::1',
    '2001:db8::1', '2001::1', '2001:1::1', '3fff::1', 'fec0::1', '100::1',
    'not-an-ip', '', '1.2.3', '1.2.3.4.5', '999.1.1.1',
  ]
  for (const ip of blocked) assert.equal(isBlockedIp(ip), true, `should block ${ip}`)
  const open = ['8.8.8.8', '1.1.1.1', '172.15.0.1', '172.32.0.1', '100.63.255.255', '100.128.0.1', '192.0.1.1', '198.17.0.1', '198.20.0.1', '223.255.255.255',
    '2606:4700::1111', '2a00:1450:4009:81f::200e', '2001:4860:4860::8888', '2001:200::1']
  for (const ip of open) assert.equal(isBlockedIp(ip), false, `should allow ${ip}`)
})

// ---- fetchImage with a fake resolver and transport (no network) ----
const resp = (body, { status = 200, headers = {} } = {}) => {
  const r = Readable.from(Array.isArray(body) ? body : [body])
  r.statusCode = status; r.headers = { 'content-type': 'image/png', ...headers }
  return r
}
const rig = (over = {}) => {
  const seen = { resolved: [], requests: [] }
  return {
    seen,
    opts: {
      allowlist: ALLOW,
      resolve: over.resolve || (async (h) => { seen.resolved.push(h); return [{ address: '104.18.0.1', family: 4 }] }),
      request: over.request || (async (url, o) => { seen.requests.push({ url: url.href, o }); return resp(ImgBuf('image/png', 100)) }),
      ...over.extra,
    },
  }
}

test('fetchImage: happy path returns bytes and the mime; connection is pinned to the validated address', async () => {
  const { opts, seen } = rig()
  const r = await fetchImage(OK, opts)
  assert.equal(r.mime, 'image/png'); assert.equal(r.buf.length, 100)
  assert.deepEqual(seen.resolved, ['res.cloudinary.com'])
  // the transport's lookup is the pinned one: whatever the TLS stack asks for, it only ever gets the vetted address
  const lookup = seen.requests[0].o.lookup
  assert.deepEqual(await new Promise((res) => lookup('res.cloudinary.com', { all: true }, (e, a) => res(a))), [{ address: '104.18.0.1', family: 4 }])
  assert.deepEqual(await new Promise((res) => lookup('anything-else.evil', {}, (e, a, f) => res([a, f]))), ['104.18.0.1', 4])
  assert.ok(seen.requests[0].o.signal)
})

test('fetchImage: jpeg, webp and gif are accepted; content-type parameters are ignored', async () => {
  for (const t of ['image/jpeg', 'image/webp', 'image/gif']) {
    const { opts } = rig({ request: async () => resp(ImgBuf(t, 80), { headers: { 'content-type': `${t.toUpperCase()}; charset=binary` } }) })
    assert.equal((await fetchImage(OK, opts)).mime, t)
  }
})

test('fetchImage: DNS answers pointing at private, loopback, link-local or metadata addresses are refused, even mixed with a public one', async () => {
  for (const address of ['127.0.0.1', '10.1.2.3', '169.254.169.254', '192.168.1.1', '::1', 'fd00::1', '::ffff:127.0.0.1']) {
    const { opts, seen } = rig({ resolve: async () => [{ address, family: address.includes(':') ? 6 : 4 }] })
    await assert.rejects(fetchImage(OK, opts), code(400, 'invalid_image_url'), address)
    assert.equal(seen.requests.length, 0, 'no connection may be attempted')
  }
  const { opts, seen } = rig({ resolve: async () => [{ address: '104.18.0.1', family: 4 }, { address: '10.0.0.9', family: 4 }] })
  await assert.rejects(fetchImage(OK, opts), code(400, 'invalid_image_url'))
  assert.equal(seen.requests.length, 0)
  for (const empty of [[], null, undefined]) await assert.rejects(fetchImage(OK, rig({ resolve: async () => empty }).opts), code(400, 'invalid_image_url'))
})

test('fetchImage: a failing resolver or transport is a 502, never a hang or a crash', async () => {
  await assert.rejects(fetchImage(OK, rig({ resolve: async () => { throw new Error('ENOTFOUND') } }).opts), code(502, 'image_fetch_failed'))
  await assert.rejects(fetchImage(OK, rig({ request: async () => { throw new Error('ECONNRESET') } }).opts), code(502, 'image_fetch_failed'))
})

test('fetchImage: redirects are never followed (every 3xx is refused, the Location is not requested)', async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    const { opts, seen } = rig({ request: async (u, o) => { seen.requests.push(u.href); return resp('', { status, headers: { location: 'http://169.254.169.254/latest' } }) } })
    await assert.rejects(fetchImage(OK, opts), code(400, 'invalid_image_url'), String(status))
    assert.equal(seen.requests.length, 1)
  }
})

test('fetchImage: an upstream 5xx/429 is transient (502 image_fetch_failed), other non-200s are 400', async () => {
  for (const status of [429, 500, 502, 503]) await assert.rejects(fetchImage(OK, rig({ request: async () => resp(ImgBuf(), { status }) }).opts), code(502, 'image_fetch_failed'), String(status))
})

test('fetchImage: the deadline also covers a hanging DNS lookup', async () => {
  const t0 = Date.now()
  await assert.rejects(fetchImage(OK, rig({ extra: { timeoutMs: 40 }, resolve: () => new Promise(() => {}) }).opts), code(502, 'image_fetch_failed'))
  assert.ok(Date.now() - t0 < 1000)
})

test('fetchImage: non-200 statuses are refused', async () => {
  for (const status of [204, 206, 400, 403, 404]) await assert.rejects(fetchImage(OK, rig({ request: async () => resp(ImgBuf(), { status }) }).opts), code(400, 'invalid_image_url'), String(status))
})

test('fetchImage: wrong or missing content-type is 415, and so is a lying one (bytes are not that image)', async () => {
  for (const ct of ['text/html', 'application/octet-stream', 'image/svg+xml', 'image/heic', 'image/bmp', '', undefined]) {
    const { opts } = rig({ request: async () => resp(ImgBuf(), { headers: { 'content-type': ct } }) })
    await assert.rejects(fetchImage(OK, opts), code(415, 'unsupported_media_type'), String(ct))
  }
  await assert.rejects(fetchImage(OK, rig({ request: async () => resp(Buffer.from('<html>not an image at all</html>')) }).opts), code(415, 'unsupported_media_type'))
  await assert.rejects(fetchImage(OK, rig({ request: async () => resp(ImgBuf('image/jpeg', 50)) }).opts), code(415, 'unsupported_media_type'))   // jpeg bytes labelled png
})

test('fetchImage: content-encoding other than identity is refused (no decompression bombs)', async () => {
  for (const enc of ['gzip', 'br', 'deflate']) await assert.rejects(fetchImage(OK, rig({ request: async () => resp(ImgBuf(), { headers: { 'content-encoding': enc } }) }).opts), code(415, 'unsupported_media_type'))
})

test('fetchImage: oversized is 413 whether declared or not; the cap counts RECEIVED bytes and the stream is cut early', async () => {
  const cap = { maxBytes: 1000 }
  // declared too large
  await assert.rejects(fetchImage(OK, rig({ extra: cap, request: async () => resp(ImgBuf('image/png', 100), { headers: { 'content-length': '999999' } }) }).opts), code(413, 'image_too_large'))
  // lying declared length (small) but more bytes actually arrive
  let produced = 0
  const body = Readable.from((function* () { for (;;) { produced++; yield ImgBuf('image/png', 400) } })())
  const r = resp([]); r.destroy(); body.statusCode = 200; body.headers = { 'content-type': 'image/png', 'content-length': '10' }
  await assert.rejects(fetchImage(OK, rig({ extra: cap, request: async () => body }).opts), code(413, 'image_too_large'))
  assert.ok(produced <= 4); assert.ok(body.destroyed)
  // exactly at the cap is fine
  assert.equal((await fetchImage(OK, rig({ extra: cap, request: async () => resp(ImgBuf('image/png', 1000)) }).opts)).buf.length, 1000)
})

test('fetchImage: the hard deadline aborts a stalled response (signal fires, 502)', async () => {
  let signal
  const stalled = new Readable({ read() {} }); stalled.statusCode = 200; stalled.headers = { 'content-type': 'image/png' }
  const { opts } = rig({ extra: { timeoutMs: 40 }, request: async (u, o) => { signal = o.signal; o.signal.addEventListener('abort', () => stalled.destroy(new Error('aborted'))); return stalled } })
  await assert.rejects(fetchImage(OK, opts), code(502, 'image_fetch_failed'))
  assert.ok(signal.aborted)
})

test('fetchImage: validates the URL itself too (defence in depth) before any DNS or network', async () => {
  const { opts, seen } = rig()
  await assert.rejects(fetchImage('http://res.cloudinary.com/a.png', opts), code(400, 'invalid_image_url'))
  await assert.rejects(fetchImage('https://evil.com/a.png', opts), code(400, 'invalid_image_url'))
  assert.equal(seen.resolved.length, 0); assert.equal(seen.requests.length, 0)
})
