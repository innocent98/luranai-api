// Spike sidecar: one WhatsApp linked-device session via Baileys, driven over a tiny HTTP API.
// Safety rails (spike-only): binds 127.0.0.1; refuses to pair/send to any number except SPIKE_TEST_NUMBER;
// replies only to chats that messaged us first; per-minute rate cap + human-like delay.
import http from 'node:http'
import { createHash, randomUUID, timingSafeEqual } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import pino from 'pino'
import makeWASocket, {
  Browsers, DisconnectReason, downloadMediaMessage, fetchLatestBaileysVersion, makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys'
import { useEncryptedAuthState } from './crypto-store.mjs'
import { sign } from './sign.mjs'
import { MediaTooLarge, declaredLength, readCapped } from './media.mjs'

const env = (k, d) => process.env[k] ?? d
const PORT = Number(env('PORT', 8787))
const TEST_NUMBER = env('SPIKE_TEST_NUMBER')
const AUTH_KEY = env('SPIKE_AUTH_KEY')
const SECRET = env('SPIKE_WEBHOOK_SECRET')
const TOKEN = env('SPIKE_API_TOKEN')
const WEBHOOK_URL = env('WEBHOOK_URL', 'http://127.0.0.1:8788/hook')
const AUTH_DIR = env('AUTH_DIR', '.auth/session1')
const MEDIA_DIR = env('MEDIA_DIR', '.media')
const MAX_PER_MIN = Number(env('MAX_SENDS_PER_MIN', 6))
for (const [k, v] of Object.entries({ SPIKE_TEST_NUMBER: TEST_NUMBER, SPIKE_AUTH_KEY: AUTH_KEY, SPIKE_WEBHOOK_SECRET: SECRET, SPIKE_API_TOKEN: TOKEN })) {
  if (!v) { console.error(`missing env ${k}`); process.exit(2) }
}
// Message ids come from the remote sender: never use one as a path without validating it.
const SAFE_ID = /^[A-Za-z0-9_-]{1,64}$/
const mediaPath = (id) => {
  if (!SAFE_ID.test(id)) return null
  const base = path.resolve(MEDIA_DIR), dest = path.resolve(base, id)
  return path.dirname(dest) === base ? dest : null
}
const mask = (n) => (n ? `+${String(n).slice(0, 3)}***${String(n).slice(-3)}` : n)
const log = pino({ level: 'info', redact: ['*.number', '*.to', '*.from'] })
const baileysLog = pino({ level: 'silent' })

let sock = null
let saveCreds = null
let authWipe = null
const S = { status: 'idle', since: Date.now(), history: [], pairing: null, lastDisconnect: null }
const seen = new Set()          // inbound message-id dedupe
const inboundChats = new Set()  // reply-only allowlist: JIDs that wrote to us
const consented = new Set()     // spike: JIDs the operator confirmed are their own phone (POST /allow {messageId})
const inboundById = new Map()  // messageId -> jid, so the operator names the exact message to consent to (no 'last inbound' race)
const MAX_MEDIA_BYTES = 10 * 1048576
const sendTimes = []
const setStatus = (status, extra = {}) => {
  S.history.push({ status, at: new Date().toISOString(), ...extra })
  S.status = status; S.since = Date.now()
  log.info({ status, ...extra }, 'status')
  deliver({ type: 'session.status', status, ...extra })
}

async function deliver(event) {
  const body = JSON.stringify({ id: randomUUID(), at: new Date().toISOString(), ...event })
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(WEBHOOK_URL, { method: 'POST', body, headers: { 'content-type': 'application/json', 'x-luran-signature': sign(SECRET, body) } })
      if (r.ok) return
    } catch { /* receiver down: retry */ }
    await new Promise((r) => setTimeout(r, 500 * 2 ** i))
  }
  log.warn({ type: event.type }, 'webhook delivery failed after retries')
}

const textOf = (m) => m?.conversation || m?.extendedTextMessage?.text || m?.imageMessage?.caption || ''
async function onMessages({ messages, type }) {
  if (type !== 'notify') return
  for (const msg of messages) {
    const jid = msg.key.remoteJid || ''
    if (msg.key.fromMe || !jid.endsWith('@s.whatsapp.net') && !jid.endsWith('@lid')) continue // 1:1 only, no groups/status
    if (!msg.key.id || !SAFE_ID.test(msg.key.id)) { log.warn('dropping message with unexpected id format'); continue }
    if (seen.has(msg.key.id)) continue
    seen.add(msg.key.id)
    inboundChats.add(jid); inboundById.set(msg.key.id, jid)
    if (inboundById.size > 500) inboundById.delete(inboundById.keys().next().value)
    const m = msg.message || {}
    const ev = { type: 'message.inbound', messageId: msg.key.id, from: jid, fromMasked: mask(jid.split('@')[0]), kind: 'other', text: textOf(m) }
    if (m.imageMessage && declaredLength(m.imageMessage.fileLength) > MAX_MEDIA_BYTES) {
      ev.kind = 'other'; ev.skipped = 'media_too_large'   // size cap enforced before any download
    } else if (m.imageMessage) {
      ev.kind = 'image'; ev.mime = m.imageMessage.mimetype
      let buf
      try {
        const stream = await downloadMediaMessage(msg, 'stream', {}, { logger: baileysLog, reuploadRequest: sock.updateMediaMessage })
        buf = await readCapped(stream, MAX_MEDIA_BYTES)   // cap enforced on received bytes
      } catch (e) {
        if (!(e instanceof MediaTooLarge)) throw e
        ev.kind = 'other'; ev.skipped = 'media_too_large'
      }
      if (buf) {
        await fs.mkdir(MEDIA_DIR, { recursive: true, mode: 0o700 })
        await fs.writeFile(mediaPath(msg.key.id), buf, { mode: 0o600, flag: 'wx' })
        ev.bytes = buf.length; ev.sha256 = createHash('sha256').update(buf).digest('hex'); ev.mediaUrl = `/media/${msg.key.id}`
      }
    } else if (m.conversation || m.extendedTextMessage) ev.kind = 'text'
    log.info({ kind: ev.kind, from: ev.fromMasked, bytes: ev.bytes }, 'inbound')
    deliver(ev)
  }
}

async function start({ pairNumber } = {}) {
  const store = await useEncryptedAuthState(AUTH_DIR, AUTH_KEY)
  saveCreds = store.saveCreds; authWipe = store.wipe
  const { version } = await fetchLatestBaileysVersion()
  setStatus('connecting', { registered: !!store.state.creds.registered })
  sock = makeWASocket({
    version, logger: baileysLog,
    auth: { creds: store.state.creds, keys: makeCacheableSignalKeyStore(store.state.keys, baileysLog) },
    browser: Browsers.macOS('Chrome'),   // valid [platform, browser, version] tuple; custom names break pairing
    syncFullHistory: false, markOnlineOnConnect: false, generateHighQualityLinkPreview: false,
  })
  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('messages.upsert', (u) => onMessages(u).catch((e) => log.error({ err: e.message }, 'inbound handler')))
  let codeRequested = false
  sock.ev.on('connection.update', async (u) => {
    const { connection, lastDisconnect, qr } = u
    // Request the code only once, only when unregistered, only after the socket is ready (first qr event)
    if (qr && pairNumber && !store.state.creds.registered && !codeRequested) {
      codeRequested = true
      const t0 = Date.now()
      try {
        const code = await sock.requestPairingCode(pairNumber)
        S.pairing = { code, issuedAt: Date.now(), ms: Date.now() - t0 }
        setStatus('awaiting_pair', { codeMs: S.pairing.ms })
      } catch (e) { S.pairing = { error: e.message }; log.error({ err: e.message }, 'requestPairingCode failed') }
    }
    if (connection === 'open') { S.pairing = null; setStatus('connected', { registered: true }) }
    if (connection === 'close') {
      const status = lastDisconnect?.error?.output?.statusCode
      S.lastDisconnect = { status, at: new Date().toISOString(), msg: lastDisconnect?.error?.message }
      if (status === DisconnectReason.loggedOut) {
        await authWipe()                       // unlinked from phone: creds are dead, discard them
        setStatus('logged_out', { code: status })
        sock = null
      } else {
        setStatus('reconnecting', { code: status })
        setTimeout(() => start({ pairNumber: store.state.creds.registered ? undefined : pairNumber }).catch((e) => log.error({ err: e.message }, 'restart')), status === DisconnectReason.restartRequired ? 0 : 2000)
      }
    }
  })
}

// ---- send with reply-only guard, rate cap, human-like delay ----
async function sendText(to, text) {
  const digits = String(to).replace(/\D/g, '')
  const jid = [...inboundChats].find((j) => j.split('@')[0] === digits) || null
  if (!jid) throw Object.assign(new Error('reply-only: contact has not messaged us'), { code: 409 })
  if (!consented.has(jid)) throw Object.assign(new Error('recipient not consented for spike'), { code: 403 })
  if (S.status !== 'connected') throw Object.assign(new Error(`session ${S.status}`), { code: 503 })
  const now = Date.now()
  while (sendTimes.length && now - sendTimes[0] > 60000) sendTimes.shift()
  if (sendTimes.length >= MAX_PER_MIN) throw Object.assign(new Error('rate cap'), { code: 429 })
  sendTimes.push(now)
  await sock.presenceSubscribe(jid)
  await sock.sendPresenceUpdate('composing', jid)
  await new Promise((r) => setTimeout(r, 1500 + Math.random() * 2500 + Math.min(text.length * 25, 3000)))
  await sock.sendPresenceUpdate('paused', jid)
  const res = await sock.sendMessage(jid, { text })
  return { messageId: res?.key?.id }
}

const ok = (res, code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)) }
const readJson = (req) => new Promise((resolve) => { let b = ''; req.on('data', (c) => (b += c)); req.on('end', () => { try { resolve(JSON.parse(b || '{}')) } catch { resolve({}) } }) })
const authed = (req) => { const h = Buffer.from(req.headers.authorization || ''); const w = Buffer.from(`Bearer ${TOKEN}`); return h.length === w.length && timingSafeEqual(h, w) }

http.createServer(async (req, res) => {
  if (!authed(req)) return ok(res, 401, { error: 'unauthorized' })
  try {
    if (req.method === 'POST' && req.url === '/pair') {
      const { number } = await readJson(req)
      const digits = String(number || '').replace(/\D/g, '')
      if (digits !== TEST_NUMBER) return ok(res, 403, { error: 'number not on spike allowlist' })
      if (sock) return ok(res, 409, { error: `session already ${S.status}` })
      S.pairing = null
      await start({ pairNumber: digits })
      for (let i = 0; i < 60 && !S.pairing; i++) await new Promise((r) => setTimeout(r, 500))
      return S.pairing?.code ? ok(res, 200, { code: S.pairing.code, ttlHint: 'about 3 minutes', ms: S.pairing.ms }) : ok(res, 504, { error: S.pairing?.error || 'no code within 30s', status: S.status, lastDisconnect: S.lastDisconnect })
    }
    if (req.method === 'GET' && req.url === '/status') {
      const mu = process.memoryUsage()
      return ok(res, 200, { status: S.status, sinceMs: Date.now() - S.since, rssMB: +(mu.rss / 1048576).toFixed(1), heapMB: +(mu.heapUsed / 1048576).toFixed(1), lastDisconnect: S.lastDisconnect, history: S.history.slice(-12) })
    }
    if (req.method === 'POST' && req.url === '/allow') {
      const { messageId } = await readJson(req)
      const jid = inboundById.get(String(messageId || ''))
      if (!jid) return ok(res, 404, { error: 'unknown inbound messageId' })
      consented.add(jid); return ok(res, 200, { allowed: mask(jid.split('@')[0]) })
    }
    if (req.method === 'POST' && req.url === '/send') {
      const { to, text } = await readJson(req)
      return ok(res, 200, await sendText(to, String(text || '')))
    }
    if (req.method === 'GET' && req.url?.startsWith('/media/')) {
      const file = mediaPath(path.basename(req.url))
      if (!file) return ok(res, 400, { error: 'bad media id' })
      const buf = await fs.readFile(file)
      res.writeHead(200, { 'content-type': 'application/octet-stream' }); return res.end(buf)
    }
    ok(res, 404, { error: 'not found' })
  } catch (e) { ok(res, e.code || 500, { error: e.message }) }
}).listen(PORT, '127.0.0.1', async () => {
  log.info({ port: PORT }, 'sidecar listening')
  // Restore on boot: if encrypted creds exist and are registered, reconnect without any pairing step
  const probe = await useEncryptedAuthState(AUTH_DIR, AUTH_KEY)
  if (probe.state.creds.registered) { log.info('restoring registered session'); start().catch((e) => log.error({ err: e.message }, 'restore')) }
})
