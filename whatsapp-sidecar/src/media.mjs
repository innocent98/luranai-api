// Image bytes: capped reads, the bounded in-memory store behind GET /sessions/{key}/media/{id}, and a concurrency gate.
// Bytes are never written to disk by this code and never logged. Everything here is bounded on purpose: the sidecar
// shares a small VPS (384 MiB container) with production.
import { createHash, randomBytes } from 'node:crypto'

export const MAX_IMAGE_BYTES = 10 * 1024 * 1024
export const IMAGE_MIMES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif'])

export class MediaTooLarge extends Error {}
export class MediaTimeout extends Error {}

// Protobuf lengths arrive as Long objects, numbers or undefined. Anything unusable fails closed (Infinity).
export const declaredLength = (v) => {
  if (v == null) return Infinity
  const n = typeof v === 'object' && typeof v.toNumber === 'function' ? v.toNumber() : Number(v)
  return Number.isFinite(n) && n >= 0 ? n : Infinity
}

// The size cap is enforced on bytes actually received, never on a sender-declared length.
export async function readCapped(stream, max) {
  const chunks = []
  let total = 0
  try {
    for await (const c of stream) {
      total += c.length
      if (total > max) throw new MediaTooLarge('media exceeds the cap')
      chunks.push(c)
    }
  } catch (e) {
    stream.destroy?.()
    throw e
  }
  return Buffer.concat(chunks, total)   // always a fresh, exactly-sized copy: a stored slice must not pin a larger parent buffer
}

// open() resolves to a readable stream; the whole open + read is bounded by `ms` and by `max` received bytes.
export function downloadCapped(open, max, ms) {
  return new Promise((resolve, reject) => {
    let stream, done = false
    const finish = (fn, v) => { if (done) return; done = true; clearTimeout(timer); fn(v) }
    const timer = setTimeout(() => { stream?.destroy?.(); finish(reject, new MediaTimeout('media download timed out')) }, ms)
    Promise.resolve().then(open).then(async (s) => {
      stream = s
      if (done) { s?.destroy?.(); return }
      try { finish(resolve, await readCapped(s, max)) } catch (e) { finish(reject, e) }
    }, (e) => finish(reject, e))
  })
}

export function withTimeout(promise, ms, makeError) {
  let timer
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(makeError()), ms) })
  return Promise.race([promise, limit]).finally(() => clearTimeout(timer))
}

export const sha256Hex = (buf) => createHash('sha256').update(buf).digest('hex')

// Magic-byte check so the declared/served content-type cannot disguise other bytes as an image.
export function sniffImage(b) {
  if (!Buffer.isBuffer(b) || b.length < 12) return null
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg'
  if (b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'image/png'
  const head = b.subarray(0, 6).toString('latin1')
  if (head === 'GIF87a' || head === 'GIF89a') return 'image/gif'
  if (b.subarray(0, 4).toString('latin1') === 'RIFF' && b.subarray(8, 12).toString('latin1') === 'WEBP') return 'image/webp'
  return null
}

// At most `n` operations at once; the rest wait. Slots are handed over directly so the limit can never be exceeded.
export class Gate {
  constructor(n) { this.n = n; this.active = 0; this.q = [] }
  async run(fn) {
    if (this.active >= this.n) await new Promise((r) => this.q.push(r))
    else this.active++
    try { return await fn() } finally {
      const next = this.q.shift()
      if (next) next(); else this.active--
    }
  }
}

const ID_RE = /^[0-9a-f]{32}$/

/**
 * Bounded in-memory store for inbound images. TTL, per-session item cap and a global byte cap; eviction is oldest first.
 * Entries are bound to the session key that stored them: another key sees "not found", exactly like an unknown id.
 */
export class MediaStore {
  constructor({ maxBytes, perSession = 8, ttlMs = 600000, now = () => Date.now() }) {
    Object.assign(this, { maxBytes, perSession, ttlMs, now })
    // One session (e.g. a stranger flooding one tenant with images) may hold at most half the store, so it cannot push
    // every other tenant's images out before their backend has fetched them.
    this.perSessionBytes = Math.max(MAX_IMAGE_BYTES, Math.floor(maxBytes / 2))
    this.items = new Map()   // id -> { session, mime, buf, at }; insertion order = age order
    this.total = 0
  }

  get size() { return this.items.size }

  _drop(id) {
    const e = this.items.get(id)
    if (!e) return
    this.items.delete(id)
    this.total -= e.buf.length
  }

  // Returns the new id, or null when the item alone cannot fit the store.
  put(session, mime, buf) {
    if (buf.length > this.maxBytes) return null
    this.sweep()
    const mine = []
    let mineBytes = 0
    for (const [id, e] of this.items) if (e.session === session) { mine.push(id); mineBytes += e.buf.length }
    while (mine.length && (mine.length >= this.perSession || mineBytes + buf.length > this.perSessionBytes)) {
      const id = mine.shift(); mineBytes -= this.items.get(id).buf.length; this._drop(id)
    }
    while (this.total + buf.length > this.maxBytes) this._drop(this.items.keys().next().value)
    const id = randomBytes(16).toString('hex')   // 128 random bits: unguessable, unrelated to the WhatsApp message id
    this.items.set(id, { session, mime, buf, at: this.now() })
    this.total += buf.length
    return id
  }

  get(session, id) {
    if (typeof id !== 'string' || !ID_RE.test(id)) return null
    const e = this.items.get(id)
    if (!e || e.session !== session) return null
    if (this.now() - e.at >= this.ttlMs) { this._drop(id); return null }
    return { mime: e.mime, buf: e.buf }
  }

  sweep() {
    const t = this.now()
    for (const [id, e] of this.items) if (t - e.at >= this.ttlMs) this._drop(id)
  }

  clearSession(session) {
    for (const [id, e] of this.items) if (e.session === session) this._drop(id)
  }
}
