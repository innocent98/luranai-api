// Internal HTTP API. Bearer auth with constant-time compare. Bind to the compose network only (never published).
// Bodies, session keys in errors, pairing codes and media bytes are never logged.
import http from 'node:http'
import { errName } from './log.mjs'
import { createHash, timingSafeEqual } from 'node:crypto'
import { ApiError } from './session.mjs'
import { SessionManager } from './manager.mjs'

const MAX_BODY = 64 * 1024
const MAX_TEXT = 4096

export function bearerOk(header, token) {
  // Hash both sides so the comparison is fixed-length and never leaks the token length.
  const a = createHash('sha256').update(String(header || '')).digest()
  const b = createHash('sha256').update(`Bearer ${token}`).digest()
  return timingSafeEqual(a, b)
}

const send = (res, status, obj) => {
  const body = JSON.stringify(obj)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body), 'cache-control': 'no-store' })
  res.end(body)
}

const readJson = (req) => new Promise((resolve, reject) => {
  let size = 0; const chunks = []
  req.on('data', (c) => {
    size += c.length
    if (size > MAX_BODY) { reject(new ApiError(413, 'payload_too_large')); req.destroy(); return }
    chunks.push(c)
  })
  req.on('end', () => {
    if (!size) return resolve({})
    try {
      const v = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error()
      resolve(v)
    } catch { reject(new ApiError(400, 'bad_request', { detail: 'invalid json' })) }
  })
  req.on('error', reject)
})

export function createApi({ manager, token, log }) {
  const route = async (req, res) => {
    const url = new URL(req.url, 'http://x')
    const parts = url.pathname.split('/').filter(Boolean)
    const m = req.method
    if (parts[0] === 'health' && parts.length === 1) {
      if (m !== 'GET') throw new ApiError(405, 'method_not_allowed')
      return send(res, 200, manager.health())
    }
    if (parts[0] !== 'sessions') throw new ApiError(404, 'not_found')
    if (parts.length === 1) {
      if (m !== 'POST') throw new ApiError(405, 'method_not_allowed')
      const body = await readJson(req)
      return send(res, 201, await manager.create(body))
    }
    const key = parts[1]
    if (!SessionManager.validKey(key)) throw new ApiError(404, 'not_found')
    if (parts.length === 2) {
      if (m === 'GET') return send(res, 200, manager.get(key).snapshot())
      if (m === 'DELETE') return send(res, 200, await manager.remove(key))
      throw new ApiError(405, 'method_not_allowed')
    }
    if (parts.length === 3 && parts[2] === 'pairing-code') {
      if (m !== 'POST') throw new ApiError(405, 'method_not_allowed')
      return send(res, 200, await manager.requestPairingCode(key))
    }
    if (parts.length === 3 && parts[2] === 'send') {
      if (m !== 'POST') throw new ApiError(405, 'method_not_allowed')
      const { to_ref: to, text, image } = await readJson(req)
      if (typeof to !== 'string') throw new ApiError(400, 'bad_request', { detail: 'to_ref' })
      if (image === undefined) {
        if (typeof text !== 'string' || !text.trim() || text.length > MAX_TEXT) throw new ApiError(400, 'bad_request', { detail: 'to_ref/text' })
        return send(res, 200, await manager.send(key, to, text))
      }
      // Image send: exactly one of text/image (the caption travels in image.caption), strict shape, unknown keys refused.
      if (text !== undefined) throw new ApiError(400, 'bad_request', { detail: 'text_and_image' })
      if (!image || typeof image !== 'object' || Array.isArray(image) || Object.keys(image).some((k) => k !== 'url' && k !== 'caption')) {
        throw new ApiError(400, 'bad_request', { detail: 'image' })
      }
      if (typeof image.url !== 'string') throw new ApiError(400, 'invalid_image_url')
      if (image.caption !== undefined && (typeof image.caption !== 'string' || image.caption.length > MAX_TEXT)) {
        throw new ApiError(400, 'bad_request', { detail: 'image.caption' })
      }
      return send(res, 200, await manager.send(key, to, '', { url: image.url, caption: image.caption }))
    }
    if (parts.length === 4 && parts[2] === 'media') {
      if (m !== 'GET') throw new ApiError(405, 'method_not_allowed')
      const item = manager.media(key, parts[3])
      if (!item) throw new ApiError(404, 'not_found')   // unknown, expired and another session's id are indistinguishable
      res.writeHead(200, { 'content-type': item.mime, 'content-length': item.buf.length, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' })
      return res.end(item.buf)
    }
    throw new ApiError(404, 'not_found')
  }

  const server = http.createServer(async (req, res) => {
    if (!bearerOk(req.headers.authorization, token)) return send(res, 401, { error: 'unauthorized' })
    try { await route(req, res) } catch (e) {
      if (e instanceof ApiError) return send(res, e.status, { error: e.code, ...e.extra })
      log.error({ err: errName(e) }, 'unhandled api error')
      send(res, 500, { error: 'internal' })
    }
  })
  server.requestTimeout = 60000; server.headersTimeout = 15000; server.keepAliveTimeout = 5000
  return server
}
