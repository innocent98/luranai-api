// Server-side fetch of an outbound image URL. This is an SSRF surface, so every step is defensive:
//   1. the URL is parsed strictly (https only, no credentials, default port, no IP literals) and its host must be in the allowlist;
//   2. DNS is resolved HERE, every returned address must be public, and the connection is pinned to those exact addresses
//      (a second lookup by the TLS stack could return something else: DNS rebinding);
//   3. redirects are never followed (a redirect is an error), the whole call has a hard deadline, the body is capped on
//      RECEIVED bytes, content-encoding other than identity is refused (no decompression bombs), the content-type must be
//      an allowed image type and the first bytes must match it.
// Nothing is logged here; callers log error classes only.
import dns from 'node:dns/promises'
import https from 'node:https'
import net from 'node:net'
import { IMAGE_MIMES, MAX_IMAGE_BYTES, MediaTooLarge, readCapped, sniffImage } from './media.mjs'
import { ApiError } from './session.mjs'

const MAX_URL_LEN = 2048
const bad = () => new ApiError(400, 'invalid_image_url')

export function validateImageUrl(raw, allowlist) {
  if (typeof raw !== 'string' || raw.length === 0 || raw.length > MAX_URL_LEN || /[\s\u0000-\u001f\u007f]/.test(raw)) throw bad()
  let u
  try { u = new URL(raw) } catch { throw bad() }
  if (u.protocol !== 'https:' || u.username || u.password || u.port) throw bad()   // :443 normalises to '' so only the default port passes
  const host = u.hostname.toLowerCase()
  if (!host || host.endsWith('.') || net.isIP(host.replace(/^\[|\]$/g, ''))) throw bad()
  if (!allowlist.includes(host)) throw bad()
  return u
}

const v4 = (ip) => ip.split('.').map(Number)
function blockedV4([a, b, c]) {
  return a === 0 || a === 10 || a === 127 || a >= 224
    || (a === 100 && b >= 64 && b <= 127)             // carrier-grade NAT
    || (a === 169 && b === 254)                         // link-local, cloud metadata (169.254.169.254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 0 && (c === 0 || c === 2))   // IETF protocol assignments, TEST-NET-1
    || (a === 192 && b === 88 && c === 99)              // 6to4 relay
    || (a === 192 && b === 168)
    || (a === 198 && (b === 18 || b === 19))            // benchmarking
    || (a === 198 && b === 51 && c === 100)            // TEST-NET-2
    || (a === 203 && b === 0 && c === 113)             // TEST-NET-3
}

// Expand an IPv6 literal to 8 hextets (handles '::' and an embedded dotted quad). null when malformed.
function hextets(ip) {
  if (ip.includes('%')) return null
  let [head, tail, extra] = ip.split('::')
  if (extra !== undefined) return null
  const toParts = (s) => (s ? s.split(':') : [])
  let h = toParts(head); let t = tail === undefined ? null : toParts(tail)
  const last = (t ?? h).at(-1)
  if (last && last.includes('.')) {
    const q = v4(last); const arr = t ?? h
    arr.splice(-1, 1, ((q[0] << 8) | q[1]).toString(16), ((q[2] << 8) | q[3]).toString(16))
  }
  if (t === null) { if (h.length !== 8) return null } else {
    const fill = 8 - h.length - t.length
    if (fill < 0) return null
    h = [...h, ...Array(fill).fill('0'), ...t]
  }
  const out = h.map((x) => parseInt(x, 16))
  return out.length === 8 && out.every((n) => Number.isInteger(n) && n >= 0 && n <= 0xffff) ? out : null
}

/** true when the address must NOT be fetched: anything that is not a plain public unicast address. */
export function isBlockedIp(ip) {
  const family = net.isIP(ip)
  if (family === 4) return blockedV4(v4(ip))
  if (family !== 6) return true
  const h = hextets(ip)
  if (!h) return true
  // Positive rule: only global unicast 2000::/3 is allowed. That alone excludes ::, ::1, ::ffff:a.b.c.d (v4-mapped),
  // 64:ff9b::/96 (NAT64), fc00::/7, fe80::/10, ff00::/8 and the other special blocks; carve out the exceptions inside it.
  if ((h[0] & 0xe000) !== 0x2000) return true
  if (h[0] === 0x2001 && h[1] < 0x200) return true      // 2001::/23 incl. Teredo
  if (h[0] === 0x2001 && h[1] === 0x0db8) return true   // documentation
  if (h[0] === 0x2002) return true                      // 6to4 (embeds an IPv4 address)
  if (h[0] === 0x3fff && h[1] < 0x1000) return true     // documentation
  return false
}

const pinnedLookup = (addrs) => (_host, opts, cb) => {
  if (typeof opts === 'function') { cb = opts; opts = {} }
  if (opts?.all) cb(null, addrs.map((a) => ({ address: a.address, family: a.family })))
  else cb(null, addrs[0].address, addrs[0].family)
}

const defaultResolve = (host) => dns.lookup(host, { all: true, verbatim: true })

function defaultRequest(url, { lookup, signal }) {
  return new Promise((resolve, reject) => {
    const req = https.request(url, {
      method: 'GET', lookup, signal, agent: false,
      headers: { accept: 'image/jpeg,image/png,image/webp,image/gif', 'accept-encoding': 'identity', 'user-agent': 'luran-whatsapp-sidecar' },
    }, resolve)
    req.on('error', reject)
    req.end()
  })
}

/**
 * @returns {Promise<{buf: Buffer, mime: string}>}
 * @throws ApiError 400 invalid_image_url | 413 image_too_large | 415 unsupported_media_type | 502 image_fetch_failed
 */
export async function fetchImage(rawUrl, { allowlist, resolve = defaultResolve, request = defaultRequest, timeoutMs = 10000, maxBytes = MAX_IMAGE_BYTES } = {}) {
  const url = validateImageUrl(rawUrl, allowlist)
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  let res
  try {
    let addrs
    const expired = new Promise((_, reject) => ac.signal.addEventListener('abort', () => reject(new Error('deadline')), { once: true }))
    expired.catch(() => {})
    try { addrs = await Promise.race([resolve(url.hostname), expired]) } catch { throw new ApiError(502, 'image_fetch_failed') }   // the deadline covers DNS as well
    if (!Array.isArray(addrs) || !addrs.length || addrs.some((a) => isBlockedIp(a.address))) throw bad()   // one bad answer poisons the lot
    try { res = await request(url, { lookup: pinnedLookup(addrs), signal: ac.signal }) } catch { throw new ApiError(502, 'image_fetch_failed') }
    const status = res.statusCode
    if (status === 429 || status >= 500) throw new ApiError(502, 'image_fetch_failed')   // the host is struggling: transient, the caller may retry
    if (status !== 200) throw bad()                                          // includes every 3xx: redirects are never followed
    const enc = String(res.headers['content-encoding'] || 'identity').toLowerCase()
    if (enc !== 'identity') throw new ApiError(415, 'unsupported_media_type')
    const type = String(res.headers['content-type'] || '').split(';')[0].trim().toLowerCase()
    if (!IMAGE_MIMES.has(type)) throw new ApiError(415, 'unsupported_media_type')
    const declared = Number(res.headers['content-length'])
    if (Number.isFinite(declared) && declared > maxBytes) throw new ApiError(413, 'image_too_large')   // early out only; received bytes are the real guard
    let buf
    try { buf = await readCapped(res, maxBytes) } catch (e) {
      if (e instanceof MediaTooLarge) throw new ApiError(413, 'image_too_large')
      throw new ApiError(502, 'image_fetch_failed')
    }
    if (sniffImage(buf) !== type) throw new ApiError(415, 'unsupported_media_type')
    return { buf, mime: type }
  } finally {
    clearTimeout(timer)
    res?.destroy?.()   // no-op once the body has been consumed; stops a half-read response otherwise
  }
}
