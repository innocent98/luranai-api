// Fake-socket harness: no network, no Baileys socket. Real auth-state/crypto code runs against MemoryAuthDb.
import { Writable } from 'node:stream'
import pino from 'pino'
import { openAuthState } from '../src/auth-state.mjs'
import { MemoryAuthDb } from '../src/db.mjs'
import { MemoryLeaseBackend } from '../src/lease.mjs'
import { MemoryCounter } from '../src/limiter.mjs'
import { SessionManager } from '../src/manager.mjs'

export const MASTER = 'ab'.repeat(32)
export const TOKEN = 't'.repeat(40)
export const SECRET = 's'.repeat(40)

export class FakeSocket {
  constructor(opts, { autoQr = true } = {}) {
    this.opts = opts
    const handlers = new Map()
    this.ev = {
      on: (e, f) => { (handlers.get(e) || handlers.set(e, []).get(e)).push(f) },
      removeAllListeners: (e) => handlers.delete(e),
      emit: (e, d) => (handlers.get(e) || []).slice().forEach((f) => f(d)),
      count: (e) => (handlers.get(e) || []).length,
    }
    this.calls = []; this.ended = false; this.loggedOut = false
    this.user = { id: '15550001111:3@s.whatsapp.net', lid: '999111@lid' }
    this.lidMap = {}
    this.signalRepository = { lidMapping: { getPNForLID: async (l) => this.lidMap[l] || null } }
    this.pairCode = 'ABCD1234'
    if (autoQr) setImmediate(() => this.update({ qr: 'qr-ref' }))
  }
  update(u) { this.ev.emit('connection.update', u) }
  open() { this.update({ connection: 'open' }) }
  close(code, msg = 'closed') { this.update({ connection: 'close', lastDisconnect: { error: { output: { statusCode: code }, message: msg } } }) }
  upsert(messages, type = 'notify') { this.ev.emit('messages.upsert', { messages, type }) }
  async requestPairingCode(phone) { this.calls.push(['pair', phone]); return this.pairCode }
  async sendMessage(jid, content, o) { this.calls.push(['send', jid, content.text, o?.messageId]); return { key: { id: o?.messageId, remoteJid: jid, fromMe: true } } }
  async readMessages(keys) { this.calls.push(['read', keys[0]?.id]) }
  async presenceSubscribe(j) { this.calls.push(['sub', j]) }
  async sendPresenceUpdate(s, j) { this.calls.push(['presence', s, j]) }
  async end() { this.ended = true }
  async logout() { this.loggedOut = true; this.calls.push(['logout']) }
  names() { return this.calls.map((c) => c[0]) }
}

export const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms))
export async function until(fn, ms = 2000) {
  const end = Date.now() + ms
  while (Date.now() < end) { if (await fn()) return; await tick(2) }
  throw new Error('until: timed out')
}

export function makeLogCapture() {
  const lines = []
  const stream = new Writable({ write(chunk, _e, cb) { lines.push(chunk.toString()); cb() } })
  return { log: pino({ level: 'debug' }, stream), text: () => lines.join('') }
}

export function makeHarness(over = {}) {
  const cfg = {
    masterKeyHex: MASTER, maxSessions: 3, rssRefuseMb: 400, sendPerMin: 6, dailySendCap: 200, replyWindowHours: 24,
    restoreStaggerMs: 2000, leaseTtlMs: 30000, pairingTtlMs: 600000, pairingWaitMs: 1000, ...over.cfg,
  }
  const db = over.db || new MemoryAuthDb()
  const leaseBackend = over.leaseBackend || new MemoryLeaseBackend(() => h.clock.now)
  const logCap = makeLogCapture()
  const h = {
    cfg, db, leaseBackend, logCap, emitted: [], sockets: [], sleeps: [],
    clock: { now: 1_800_000_000_000 },
    rss: 150 * 1048576,
  }
  const deps = {
    cfg, log: logCap.log, db, leaseBackend,
    emit: (key, event) => h.emitted.push({ session_key: key, ...event }),
    makeSocket: (opts) => { const s = new FakeSocket(opts, over.socketOpts); h.sockets.push(s); return s },
    openAuth: openAuthState,
    counter: over.counter || new MemoryCounter(),
    newMessageId: (() => { let n = 0; return () => `OUT${++n}` })(),
    versionProvider: async () => undefined,
    now: () => h.clock.now,
    sleep: async (ms) => { h.sleeps.push(ms) },
    random: () => 0.5,
    backoffFn: () => 5,
    rssBytes: () => h.rss,
  }
  h.deps = deps
  h.mgr = new SessionManager(deps)
  h.last = () => h.sockets[h.sockets.length - 1]
  h.events = (type) => h.emitted.filter((e) => e.type === type)
  return h
}

// create -> code -> user enters code on the phone (creds registered, 515) -> reconnect -> open
export async function pairAndConnect(h, key = 'tenant-a', phone = '2348000000001') {
  const res = await h.mgr.create({ session_key: key, phone_digits: phone })
  const s1 = h.last(), n = h.sockets.length
  s1.opts.auth.state.creds.me = { id: `${phone}:1@s.whatsapp.net` }
  s1.opts.auth.state.creds.registered = true
  s1.close(515)
  await until(() => h.sockets.length >= n + 1)
  h.last().open()
  await until(() => h.mgr.get(key).status === 'connected')
  h.last().ev.emit('creds.update', {})                // Baileys persists creds on pairing
  await h.mgr.get(key).auth.flush()
  return res
}

export const textMsg = (id, jid, text, extra = {}) => ({ key: { id, remoteJid: jid, fromMe: false }, message: { conversation: text }, pushName: 'Ada', messageTimestamp: Math.floor(1_800_000_000), ...extra })
