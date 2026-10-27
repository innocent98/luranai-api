// One WhatsApp linked-device session: lease -> encrypted auth -> Baileys socket -> signed events out, guarded sends in.
// All Baileys access goes through injected deps (makeSocket, openAuth), so this file is tested with a fake socket.
import { chatHash, deriveSessionKey } from './crypto.mjs'
import { randomBytes } from 'node:crypto'
import { Lease, LeaseHeldElsewhere, LeaseUnavailable } from './lease.mjs'
import { mask } from './log.mjs'
import { BoundedMap, BoundedSet, parseMessage, resolvePhoneDigits, userPart, isOneToOneJid } from './messages.mjs'
import { TokenBucket, DailyCap } from './limiter.mjs'
import { backoffMs, classifyDisconnect, isRegistered } from './status.mjs'

export class ApiError extends Error {
  constructor(status, code, extra = {}) { super(code); this.status = status; this.code = code; this.extra = extra }
}
class FatalAuthError extends Error {}
// Baileys/pg error messages can embed identifiers: log the class and code only.
const errName = (e) => `${e?.constructor?.name || 'Error'}${e?.code ? `:${e.code}` : ''}`

const OWNER_EVENT_MAX_AGE_S = 900
const SEND_WARMUP_MS = 3000          // after (re)connect, hold sends briefly: no burst the moment the socket opens
const MAX_QUEUED_SENDS = 10

const deferred = () => { const d = {}; d.promise = new Promise((res, rej) => { d.resolve = res; d.reject = rej }); return d }

export class Session {
  constructor(deps, { key, phone, status = 'connecting', lastDisconnect = null, registered = false }) {
    this.d = deps
    this.key = key
    this.phone = phone
    this.status = status
    this.since = deps.now()
    this.lastDisconnect = lastDisconnect
    this.epoch = 0
    this.sock = null; this.auth = null; this.lease = null
    this.timer = null; this.attempt = 0; this.restarts = 0; this.conflicts = 0
    this.pairWaiter = null; this.pairing = false; this.pairingIssuedAt = 0; this.codeRequested = false
    this.stopped = false; this.connectedAt = 0; this._registeredInDb = registered
    this.hmacKey = deriveSessionKey(deps.cfg.masterKeyHex, key)
    this.leaseToken = randomBytes(16).toString('hex')   // stable for this Session: it can regain its own key, never another owner's
    this.upsertChain = Promise.resolve()
    this.opChain = Promise.resolve(); this.gen = 0
    this.bucket = new TokenBucket({ perMin: deps.cfg.sendPerMin, now: deps.now })
    this.daily = new DailyCap({ counter: deps.counter, cap: deps.cfg.dailySendCap, now: deps.now })
    this.sentIds = new BoundedSet(1000)        // ids we sent via /send: their echoes are NOT owner activity
    this.seenIn = new BoundedSet(2000)
    this.seenOut = new BoundedSet(2000)
    this.recentChats = new BoundedMap(2000)    // chat_ref -> last inbound ms (backed by the db for restarts)
    this.lastInboundKey = new BoundedMap(500)  // chat_ref -> message key, for readMessages before replying
    this.sentContent = new BoundedMap(200)     // id -> proto content, so Baileys can answer retry receipts
    this.sendChain = Promise.resolve(); this.sendQueued = 0
  }

  get log() { return this.d.log }
  get active() { return this.status !== 'logged_out' || this.pairing }   // counts toward MAX_SESSIONS (a re-pairing session holds a slot)
  snapshot() {
    return { status: this.status, since_ms: Math.max(0, this.d.now() - this.since), last_disconnect: this.lastDisconnect }
  }

  _setStatus(status, { code, silent = false } = {}) {
    if (code !== undefined) {
      this.lastDisconnect = { code, at: new Date(this.d.now()).toISOString() }
      this.d.db.updateSession(this.key, { lastDisconnect: this.lastDisconnect }).catch(() => {})
    }
    if (status === this.status) return
    this.status = status; this.since = this.d.now()
    this.d.db.updateSession(this.key, { status }).catch(() => {})
    this.log.info({ session: this.key, status, code }, 'status')
    if (!silent) this.d.emit(this.key, { type: 'session.status', status, ...(code !== undefined ? { code } : {}) })
  }

  // ---------- operation ordering ----------
  // Everything that touches the lease, the auth rows or starts a socket runs through _serial (one at a time), and
  // every such operation captures `gen` first. _bump() invalidates all operations queued or in flight before it: a
  // reconnect that was waiting on Redis or Postgres when a logout arrived returns without starting a socket.

  _bump() {
    this.gen++
    this._clearTimer()
    this.pairWaiter?.reject(new ApiError(409, 'cancelled'))
  }
  _serial(fn) {
    const run = this.opChain.then(fn)
    this.opChain = run.catch(() => {})
    return run
  }

  // ---------- socket lifecycle ----------

  async _ensureLease() {
    if (this.lease) {
      if (!(await this.lease.verify())) throw new LeaseUnavailable('lease not provable')   // reconnecting on a lease we cannot prove is how duplicate writers happen
      return
    }
    const lease = new Lease({
      backend: this.d.leaseBackend, sessionKey: this.key, token: this.leaseToken, ttlMs: this.d.cfg.leaseTtlMs, now: this.d.now, log: this.log,
      onLost: () => this._onLeaseLost(lease),
    })
    await lease.acquire()   // LeaseUnavailable (Redis down) / LeaseHeldElsewhere: callers fail closed
    this.lease = lease      // assigned immediately: whoever bumps next releases it
  }

  async _connect(gen) {
    await this._ensureLease()
    if (gen !== this.gen || this.stopped) return false
    if (!this.auth) {
      let auth
      try { auth = await this.d.openAuth({ db: this.d.db, sessionKey: this.key, masterHex: this.d.cfg.masterKeyHex, log: this.log }) }
      catch (e) { throw new FatalAuthError(errName(e)) }
      if (gen !== this.gen) { auth.abandon(); return false }   // credentials may have been wiped while we were reading them
      this.auth = auth
    }
    const version = await this.d.versionProvider()
    if (gen !== this.gen || this.stopped) return false
    const epoch = ++this.epoch
    const auth = this.auth
    const sock = this.d.makeSocket({ auth, version, getMessage: async (k) => this.sentContent.get(k?.id) })
    this.sock = sock; this.codeRequested = false
    sock.ev.on('creds.update', () => { if (epoch === this.epoch) auth.saveCreds().catch(() => {}) })
    sock.ev.on('connection.update', (u) => { this._onConnection(epoch, u).catch((e) => this.log.error({ session: this.key, err: errName(e) }, 'connection handler')) })
    // Serialised: lid lookups are async, and the backend must see a chat's messages in the order they arrived.
    sock.ev.on('messages.upsert', (u) => {
      this.upsertChain = this.upsertChain.then(() => this._onUpsert(epoch, u)).catch((e) => this.log.error({ session: this.key, err: errName(e) }, 'inbound handler'))
    })
    return true
  }

  _teardownSocket() {
    this.epoch++   // events still in flight from the old socket are now ignored
    const s = this.sock; this.sock = null
    if (!s) return
    try { for (const ev of ['connection.update', 'creds.update', 'messages.upsert']) s.ev.removeAllListeners?.(ev) } catch { /* ignore */ }
    try { Promise.resolve(s.end(undefined)).catch(() => {}) } catch { /* ignore */ }
  }

  _clearTimer() { if (this.timer) { clearTimeout(this.timer); this.timer = null } }

  async _releaseLease() {
    const l = this.lease; this.lease = null
    if (l) await l.release()
  }

  // Close auth cleanly (flush) and free the lease. Credentials stay in the database. The final flush happens only if
  // we can still prove we own the lease; otherwise pending writes are dropped rather than risk overwriting the owner's.
  async _park() {
    this._teardownSocket()
    const a = this.auth; this.auth = null
    if (a) {
      const owned = this.lease ? await this.lease.verify() : false
      if (owned) { try { await a.close() } catch (e) { this.log.warn({ session: this.key, err: errName(e) }, 'auth flush on park failed') } }
      else a.abandon()
    }
    await this._releaseLease()
  }

  // Delete every credential row. Callers must hold the lease (or have just verified it): this never runs "blind".
  async _wipeRows() {
    this._teardownSocket()
    const a = this.auth; this.auth = null
    if (a) await a.wipe(); else await this.d.db.wipeAuth(this.key)
  }

  _onLeaseLost(lease) {
    if (lease !== this.lease || this.stopped) return   // a stale Lease object must never tear down the current one
    this.log.warn({ session: this.key }, 'lease lost: socket stopped, credentials untouched')
    this._bump()
    this._teardownSocket()
    this.auth?.abandon(); this.auth = null    // never flush after losing ownership
    this.lease = null
    this._afterStop(440)
  }

  // A socket ended for a non-terminal reason: registered sessions retry (re-acquiring the lease first), pairing ones do not.
  _afterStop(code) {
    if (!isRegistered(this.auth?.state?.creds) && !this._registeredInDb) {
      if (this.status === 'connecting') this._setStatus('awaiting_pair', { silent: true })
      return
    }
    this._setStatus('reconnecting', { code })
    this._scheduleReconnect()
  }

  _scheduleReconnect() {
    if (this.timer || this.stopped) return
    const delay = (this.d.backoffFn || backoffMs)(this.attempt++, this.d.random)
    this.timer = setTimeout(() => { this.timer = null; this._connectSafe().catch(() => {}) }, delay)
    this.timer.unref?.()
  }

  _connectSafe() {
    const gen = this.gen
    return this._serial(async () => {
      if (gen !== this.gen || this.stopped) return
      try { await this._connect(gen) } catch (e) {
        if (gen !== this.gen) return
        if (e instanceof LeaseHeldElsewhere) { this._setStatus('reconnecting', { code: 440 }); this._scheduleReconnect() }
        else if (e instanceof LeaseUnavailable) { this._setStatus('reconnecting', { code: 503 }); this._scheduleReconnect() }
        else if (e instanceof FatalAuthError) {
          this.log.error({ session: this.key }, 'stored credentials cannot be decrypted; refusing to connect')
          await this._releaseLease(); this._setStatus('failed', { code: 500 })
        } else {
          this.log.warn({ session: this.key, err: errName(e) }, 'connect failed')
          this._setStatus('reconnecting', { code: 503 }); this._scheduleReconnect()
        }
      }
    })
  }

  async _onConnection(epoch, u) {
    if (epoch !== this.epoch) return
    const { connection, lastDisconnect, qr } = u
    if (qr && this.pairWaiter && !this.codeRequested && !isRegistered(this.auth?.state?.creds)) {
      this.codeRequested = true   // exactly one request per socket, only on an explicit API call, only when unregistered
      const w = this.pairWaiter
      try { w.resolve(await this.sock.requestPairingCode(this.phone)) } catch { w.reject(new ApiError(502, 'pairing_failed')) }
    }
    if (connection === 'open') {
      this.attempt = 0; this.restarts = 0; this.connectedAt = this.d.now()
      this._registeredInDb = true
      this.d.db.updateSession(this.key, { registered: true }).catch(() => {})
      this._setStatus('connected')
    }
    if (connection === 'close') await this._onClose(epoch, lastDisconnect?.error?.output?.statusCode)
  }

  async _onClose(epoch, code) {
    const registered = isRegistered(this.auth?.state?.creds) || !!this._registeredInDb
    if (this.pairWaiter && !this.codeRequested) this.pairWaiter.reject(new ApiError(502, 'pairing_failed'))   // socket died before it was ready
    // A 401 is only a real logout if we provably hold the lease (strictly: an evicted-and-recreated key proves nothing).
    const leaseHeld = code === 401 ? !!(this.lease && await this.lease.verify({ strict: true })) : !!this.lease?.held
    if (epoch !== this.epoch) return
    if (this.connectedAt && this.d.now() - this.connectedAt > 120000) this.conflicts = 0   // a conflict after a long healthy run is a new incident
    if (code === 440) this.conflicts++
    const d = classifyDisconnect({ code, registered, leaseHeld, consecutiveRestarts: this.restarts, consecutiveConflicts: this.conflicts })
    this.log.info({ session: this.key, code, action: d.action }, 'socket closed')
    const exclusive = (fn) => { this._bump(); return this._serial(async () => { if (epoch !== this.epoch) return; await fn() }) }
    switch (d.action) {
      case 'restart':
        this.restarts++
        this._teardownSocket(); this._setStatus('connecting')
        await this._connectSafe(); break
      case 'logout':
        await exclusive(async () => { await this._wipeRows(); await this._releaseLease(); this._setStatus('logged_out', { code }) }); break
      case 'stop':
        await exclusive(async () => { await this._park(); this._setStatus(d.status, { code }) }); break
      case 'idle':
        await exclusive(async () => {
          await this._park(); this.lastDisconnect = { code: code ?? 0, at: new Date(this.d.now()).toISOString() }
          if (this.status === 'connecting') this._setStatus('awaiting_pair', { silent: true })
        }); break
      case 'lease_lost':
        this._bump(); this._teardownSocket(); this.auth?.abandon(); this.auth = null; await this._releaseLease()
        this._setStatus('reconnecting', { code: 440 }); this._scheduleReconnect(); break
      default: // reconnect
        this._teardownSocket(); this._setStatus('reconnecting', { code }); this._scheduleReconnect()
    }
  }

  // ---------- operations ----------

  async restore() {
    this._registeredInDb = true
    this._setStatus('connecting')
    await this._connectSafe()
  }

  _leaseError(e) {
    if (e instanceof LeaseHeldElsewhere) return new ApiError(409, 'session_locked')
    if (e instanceof LeaseUnavailable) return new ApiError(503, 'lease_unavailable')
    return e
  }

  /** Fresh socket -> wait for the first qr event (socket ready) -> requestPairingCode once. Never auto-retried. */
  async startPairing() {
    if (this.pairing) throw new ApiError(409, 'pairing_in_progress')
    if (this.stopped) throw new ApiError(503, 'shutting_down')
    this.pairing = true
    this._bump()
    const gen = this.gen
    const prev = this.status
    try {
      return await this._serial(async () => {
        if (gen !== this.gen) throw new ApiError(409, 'cancelled')
        try { await this._ensureLease() } catch (e) { throw this._leaseError(e) }   // lease BEFORE touching the rows
        this._teardownSocket()
        await this._wipeRows()                                  // unregistered by precondition: start from clean creds
        this._registeredInDb = false
        const w = this.pairWaiter = deferred()
        const t = setTimeout(() => w.reject(new ApiError(504, 'pairing_code_timeout')), this.d.cfg.pairingWaitMs ?? 30000)
        try {
          try { if (!(await this._connect(gen))) throw new ApiError(409, 'cancelled') } catch (e) {
            if (e instanceof ApiError) throw e
            throw e instanceof LeaseHeldElsewhere || e instanceof LeaseUnavailable ? this._leaseError(e) : new ApiError(502, 'pairing_failed')
          }
          const code = await w.promise
          this.pairingIssuedAt = this.d.now()
          this._setStatus('awaiting_pair')
          return code
        } finally { clearTimeout(t); this.pairWaiter = null }
      })
    } catch (e) {
      if (gen === this.gen) {
        await this._serial(async () => {
          this._teardownSocket()
          try { if (this.lease?.held) await this._wipeRows() } catch { /* best effort */ }
          await this._releaseLease()
        }).catch(() => {})
        this._setStatus(prev, { silent: true })
      }
      throw e
    } finally { this.pairing = false }
  }

  pairingExpired() { return this.status === 'awaiting_pair' && !this.pairing && this.d.now() - this.pairingIssuedAt > this.d.cfg.pairingTtlMs }

  async expirePairing() {
    this._bump()
    await this._serial(async () => {
      try { await this._ensureLease() } catch { return }      // not provably ours (or Redis down): leave it, the sweeper retries
      await this._wipeRows(); await this._releaseLease()
      this._setStatus('logged_out', { code: 408 })
    })
  }

  async logout() {
    this._bump()
    return this._serial(async () => {
      if (this.status === 'logged_out' && !this.sock && !this.lease && !this.pairing) return   // credentials already gone
      try { await this._ensureLease() } catch (e) {
        if (this.status === 'reconnecting') this._scheduleReconnect()
        throw this._leaseError(e)   // never wipe credentials another process may be using
      }
      const sock = this.sock
      const remote = sock && this.status === 'connected'
      this.epoch++   // ignore the close event our own logout will cause
      if (remote) { try { await Promise.race([sock.logout(), new Promise((r) => setTimeout(r, 8000))]) } catch { /* device may already be unlinked */ } }
      await this._wipeRows(); await this._releaseLease()
      this._registeredInDb = false
      this._setStatus('logged_out')
    })
  }

  // SIGTERM: stop reconnecting, flush credentials, close the socket, release the lease. No status event: the backend
  // should keep showing the last real state across a deploy.
  async stop() {
    this.stopped = true
    this._bump()
    try { await this._serial(() => this._park()) } catch (e) { this.log.warn({ session: this.key, err: errName(e) }, 'stop failed') }
  }

  // ---------- inbound / owner activity ----------

  async _onUpsert(epoch, { messages, type }) {
    if (epoch !== this.epoch || !this.sock) return
    const selfUsers = [userPart(this.sock.user?.id), userPart(this.sock.user?.lid)].filter(Boolean)
    for (const msg of messages || []) {
      const m = parseMessage(msg, { selfUsers })
      if (!m) continue
      if (m.fromMe) {
        if (this.sentIds.has(m.id) || this.seenOut.has(m.id)) continue
        this.seenOut.add(m.id)
        if (m.tsS && this.d.now() / 1000 - m.tsS > OWNER_EVENT_MAX_AGE_S) continue   // history catch-up, not live owner activity
        this.d.emit(this.key, { type: 'message.outbound_external', message_id: m.id, to_ref: m.chatRef, text: m.text })
        continue
      }
      if (type !== 'notify' || this.seenIn.has(m.id)) continue
      this.seenIn.add(m.id)
      this._noteInbound(m.chatRef, msg.key)
      const digits = await resolvePhoneDigits(msg, this.sock)
      this.log.info({ session: this.key, kind: m.kind, from: mask(m.chatRef) }, 'inbound')
      this.d.emit(this.key, {
        type: 'message.inbound', message_id: m.id, from_ref: m.chatRef, from_phone_digits: digits,
        push_name: m.pushName, kind: m.kind, text: m.text,
      })
    }
  }

  _noteInbound(chatRef, key) {
    const now = this.d.now(), prev = this.recentChats.get(chatRef)
    this.recentChats.set(chatRef, now)
    this.lastInboundKey.set(chatRef, key)
    if (!prev || now - prev > 600000) this.d.db.touchChat(this.key, chatHash(this.hmacKey, chatRef), now).catch(() => {})
  }

  async _lastInboundAt(chatRef) {
    const mem = this.recentChats.get(chatRef)
    if (mem) return mem
    const at = await this.d.db.chatAt(this.key, chatHash(this.hmacKey, chatRef))
    if (at) this.recentChats.set(chatRef, at)
    return at
  }

  // ---------- outbound ----------

  async send(toRef, text) {
    if (this.status !== 'connected' || !this.sock) throw new ApiError(503, 'not_connected')
    if (!isOneToOneJid(toRef)) throw new ApiError(400, 'bad_request', { detail: 'to_ref' })
    const at = await this._lastInboundAt(toRef)
    if (!at || this.d.now() - at > this.d.cfg.replyWindowHours * 3600000) throw new ApiError(409, 'reply_only')
    if (this.sendQueued >= MAX_QUEUED_SENDS) throw new ApiError(429, 'rate_cap')
    if (!this.bucket.tryTake()) throw new ApiError(429, 'rate_cap')
    if (!(await this.daily.tryTake(this.key))) throw new ApiError(429, 'rate_cap', { scope: 'daily' })
    this.sendQueued++
    const run = this.sendChain.then(() => this._doSend(toRef, text))   // one at a time per session (stricter than per chat)
    this.sendChain = run.catch(() => {})
    try { return await run } finally { this.sendQueued-- }
  }

  async _doSend(toRef, text) {
    const sock = this.sock
    const alive = () => this.sock === sock && this.status === 'connected'
    const warm = this.connectedAt + SEND_WARMUP_MS - this.d.now()
    if (warm > 0) await this.d.sleep(warm)
    if (!alive()) throw new ApiError(503, 'not_connected')
    const lastKey = this.lastInboundKey.get(toRef)
    if (lastKey) await sock.readMessages([lastKey]).catch(() => {})          // read receipt before the reply, like a person
    await sock.presenceSubscribe(toRef).catch(() => {})
    await sock.sendPresenceUpdate('composing', toRef).catch(() => {})
    await this.d.sleep(1500 + this.d.random() * 2500 + Math.min(text.length * 25, 3000))
    await sock.sendPresenceUpdate('paused', toRef).catch(() => {})
    if (!alive()) throw new ApiError(503, 'not_connected')
    const id = this.d.newMessageId(sock.user?.id)
    this.sentIds.add(id)                                                      // registered BEFORE sending: the echo must not look like the owner
    this.sentContent.set(id, { conversation: text })
    let res
    try { res = await sock.sendMessage(toRef, { text }, { messageId: id }) } catch (e) {
      this.log.warn({ session: this.key, err: errName(e) }, 'send failed')
      throw new ApiError(502, 'send_failed')
    }
    return { message_id: res?.key?.id || id }
  }
}
