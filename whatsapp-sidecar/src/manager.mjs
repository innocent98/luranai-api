// Owns every Session in the process: capacity guard, create/lookup/delete, staggered boot restore, graceful shutdown.
import { errName } from './log.mjs'
import { deriveSessionKey, open, seal } from './crypto.mjs'
import { validateImageUrl } from './image-fetch.mjs'
import { ApiError, Session } from './session.mjs'

const KEY_RE = /^[A-Za-z0-9_-]{1,64}$/
const PHONE_RE = /^\d{8,15}$/
const PHONE_KIND = 'phone'

export class SessionManager {
  constructor(deps) {
    this.d = deps
    this.sessions = new Map()
    this.shuttingDown = false
    this.sweeper = null
  }

  static validKey(k) { return KEY_RE.test(k || '') }

  rssMb() { return this.d.rssBytes() / 1048576 }
  activeCount() { let n = 0; for (const s of this.sessions.values()) if (s.active) n++; return n }
  health() {
    return { sessions: this.activeCount(), max_sessions: this.d.cfg.maxSessions, rss_mb: +this.rssMb().toFixed(1), rss_refuse_mb: this.d.cfg.rssRefuseMb }
  }
  _checkCapacity() {
    if (this.shuttingDown) throw new ApiError(503, 'shutting_down')
    if (this.activeCount() >= this.d.cfg.maxSessions || this.rssMb() >= this.d.cfg.rssRefuseMb) {
      throw new ApiError(503, 'capacity_exceeded', { max_sessions: this.d.cfg.maxSessions })
    }
  }

  _phoneEnc(key, phone) {
    return seal(deriveSessionKey(this.d.cfg.masterKeyHex, key), phone, key, PHONE_KIND, PHONE_KIND)
  }
  _phoneDec(key, blob) {
    return open(deriveSessionKey(this.d.cfg.masterKeyHex, key), blob, key, PHONE_KIND, PHONE_KIND)
  }

  get(key) {
    const s = this.sessions.get(key)
    if (!s) throw new ApiError(404, 'not_found')
    return s
  }

  async create({ session_key: key, phone_digits: phone }) {
    if (typeof key !== 'string' || !KEY_RE.test(key)) throw new ApiError(400, 'bad_request', { detail: 'session_key' })
    if (typeof phone !== 'string' || !PHONE_RE.test(phone)) throw new ApiError(400, 'bad_request', { detail: 'phone_digits' })
    if (this.sessions.has(key) || await this.d.db.getSession(key)) throw new ApiError(409, 'session_exists')
    for (const s of this.sessions.values()) if (s.active && s.phone === phone) throw new ApiError(409, 'phone_in_use')
    this._checkCapacity()
    const session = new Session(this.d, { key, phone })
    // Reserve the slot synchronously (before any await) so concurrent creates cannot both pass the capacity check.
    if (this.sessions.has(key)) throw new ApiError(409, 'session_exists')
    this.sessions.set(key, session)
    try {
      if (!(await this.d.db.createSession(key, this._phoneEnc(key, phone), 'connecting'))) throw new ApiError(409, 'session_exists')
      const code = await session.startPairing()
      return { status: 'awaiting_pair', pairing_code: code, expires_in_s: 180 }
    } catch (e) {
      this.sessions.delete(key)
      if (!(e instanceof ApiError && e.code === 'session_exists')) await this.d.db.deleteSession(key).catch(() => {})
      throw e
    }
  }

  async requestPairingCode(key) {
    const s = this.get(key)
    if (this.shuttingDown) throw new ApiError(503, 'shutting_down')
    if (!s.phone || (s.status !== 'awaiting_pair' && s.status !== 'logged_out')) throw new ApiError(409, 'not_pairable', { status: s.status })
    if (s.status === 'logged_out') {
      this._checkCapacity()
      for (const o of this.sessions.values()) if (o !== s && o.active && o.phone === s.phone) throw new ApiError(409, 'phone_in_use')
    }
    const code = await s.startPairing()
    return { pairing_code: code, expires_in_s: 180 }
  }

  async remove(key) {
    const s = this.sessions.get(key)
    if (!s) return { status: 'logged_out' }   // idempotent
    await s.logout()
    return { status: 'logged_out' }
  }

  async send(key, toRef, text, image = null) {
    if (this.shuttingDown) throw new ApiError(503, 'shutting_down')
    if (image) validateImageUrl(image.url, this.d.cfg.mediaUrlAllowlist)   // 400 invalid_image_url before anything is spent
    return this.get(key).send(toRef, text, image)
  }

  // Stored inbound image for GET /sessions/{key}/media/{id}; null for unknown, expired or another session's id.
  media(key, id) { return this.d.media.get(key, id) }

  // ---- boot ----
  // Sessions come from our own database. Restore is staggered (one per restoreStaggerMs + jitter) and capped by
  // MAX_SESSIONS and the RSS limit; the overflow is reported `failed`, never silently started.
  async restoreAll() {
    const rows = await this.d.db.listSessions()
    const toRestore = []
    let activeRestoring = 0
    for (const r of rows) {
      const key = r.session_key
      const lastDisconnect = r.last_disconnect_code != null ? { code: r.last_disconnect_code, at: new Date(r.last_disconnect_at).toISOString() } : null
      let phone
      try { phone = this._phoneDec(key, r.phone_enc) } catch { phone = null }
      const base = { key, phone, lastDisconnect, registered: r.registered }
      if (r.status === 'logged_out') { this.sessions.set(key, new Session(this.d, { ...base, status: 'logged_out' })); continue }
      if (!r.registered && r.status !== 'failed') {
        // Pairing never finished before the restart. Looks abandoned, but another process may still own it, so do not
        // wipe here: the sweeper expires it (lease first) and emits logged_out(408) only if we really own it.
        const s = new Session(this.d, { ...base, status: 'awaiting_pair' })
        this.sessions.set(key, s)
        continue
      }
      if (r.status === 'failed' || !phone) { this.sessions.set(key, new Session(this.d, { ...base, status: 'failed' })); continue }
      if (activeRestoring >= this.d.cfg.maxSessions) {
        const s = new Session(this.d, { ...base, status: 'connecting' })
        this.sessions.set(key, s)
        s._setStatus('failed', { code: 503 })
        this.d.log.warn({ session: key }, 'restore skipped: MAX_SESSIONS reached')
        continue
      }
      activeRestoring++
      const s = new Session(this.d, { ...base, status: 'connecting' })
      this.sessions.set(key, s)
      toRestore.push(s)
    }
    // Run in the background so the API (and /health) is up while sessions come back one by one.
    this.restoring = (async () => {
      let first = true
      for (const s of toRestore) {
        if (this.shuttingDown) return
        if (!first) await this.d.sleep(this.d.cfg.restoreStaggerMs + this.d.random() * 1000)
        first = false
        if (this.shuttingDown) return
        if (this.rssMb() >= this.d.cfg.rssRefuseMb) { s._setStatus('failed', { code: 503 }); continue }
        await s.restore().catch((e) => this.d.log.error({ session: s.key, err: errName(e) }, 'restore failed'))
      }
    })()
    return { total: rows.length, restoring: toRestore.length }
  }

  startSweeper(everyMs = 30000) {
    this.sweeper = setInterval(() => {
      for (const s of this.sessions.values()) if (s.pairingExpired()) s.expirePairing().catch(() => {})
      this.d.media.sweep()   // free expired image bytes even when nobody asks for them
    }, everyMs)
    this.sweeper.unref?.()
  }

  async shutdown() {
    this.shuttingDown = true
    if (this.sweeper) clearInterval(this.sweeper)
    await Promise.allSettled([...this.sessions.values()].map((s) => s.stop()))
  }
}
