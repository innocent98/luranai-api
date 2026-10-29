// Single-writer lease per session in Redis: `wa:lease:{session_key}` = owner token, PX ttl, renewed by a heartbeat.
// Why: two processes on the same credentials make WhatsApp answer 401/440 "conflict", which LOOKS like an unlink (observed
// in the spike) and would make a duplicate deploy log tenants out. Fail closed: if the lease cannot be proven, the socket stops.
// The key lives in a Redis that may evict (allkeys-lru): a missing key on renew is re-acquired with SET NX, never trusted blindly.
import { randomBytes } from 'node:crypto'

export const leaseKey = (sessionKey) => `wa:lease:${sessionKey}`

const RENEW_LUA = `
local v = redis.call('GET', KEYS[1])
if not v then
  if redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2], 'NX') then return 2 end
  return 0
end
if v == ARGV[1] then redis.call('PEXPIRE', KEYS[1], ARGV[2]); return 1 end
return 0`
const RELEASE_LUA = `if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0`

export class RedisLeaseBackend {
  constructor(redis) { this.redis = redis }
  // Acquire = renew-or-create for the SAME token: a session regains its own key after a transient loss, nobody else's.
  async acquire(key, token, ttlMs) { return (await this.redis.eval(RENEW_LUA, 1, key, token, ttlMs)) !== 0 }
  async renew(key, token, ttlMs) { return ({ 1: 'ok', 2: 'reacquired' })[await this.redis.eval(RENEW_LUA, 1, key, token, ttlMs)] || 'lost' }
  async release(key, token) { await this.redis.eval(RELEASE_LUA, 1, key, token) }
}

// In-memory backend for tests; `down = true` simulates Redis being unreachable (every call throws).
export class MemoryLeaseBackend {
  constructor(now = () => Date.now()) { this.now = now; this.map = new Map(); this.down = false }
  _live(key) { const e = this.map.get(key); if (e && e.exp <= this.now()) { this.map.delete(key); return null } return e || null }
  async acquire(key, token, ttlMs) {
    if (this.down) throw new Error('backend down')
    const e = this._live(key)
    if (e && e.token !== token) return false
    this.map.set(key, { token, exp: this.now() + ttlMs }); return true
  }
  async renew(key, token, ttlMs) {
    if (this.down) throw new Error('backend down')
    const e = this._live(key)
    if (!e) { this.map.set(key, { token, exp: this.now() + ttlMs }); return 'reacquired' }
    if (e.token !== token) return 'lost'
    e.exp = this.now() + ttlMs; return 'ok'
  }
  async release(key, token) {
    if (this.down) throw new Error('backend down')
    const e = this._live(key); if (e && e.token === token) this.map.delete(key)
  }
  evict(key) { this.map.delete(key) }
  steal(key, token, ttlMs) { this.map.set(key, { token, exp: this.now() + ttlMs }) }
}

export class LeaseUnavailable extends Error {}   // Redis unreachable: cannot prove ownership (fail closed)
export class LeaseHeldElsewhere extends Error {}

export class Lease {
  constructor({ backend, sessionKey, ttlMs, onLost, token, now = () => Date.now(), setInterval: si = setInterval, clearInterval: ci = clearInterval, log }) {
    Object.assign(this, { backend, sessionKey, ttlMs, onLost, now, log })
    this.key = leaseKey(sessionKey)
    this.token = token || randomBytes(16).toString('hex')
    this.held = false
    this._si = si; this._ci = ci; this._timer = null; this.lastOk = 0; this._lostFired = false
  }

  async acquire() {
    let ok
    try { ok = await this.backend.acquire(this.key, this.token, this.ttlMs) } catch (e) { throw new LeaseUnavailable(e.message) }
    if (!ok) throw new LeaseHeldElsewhere(this.sessionKey)
    this.held = true; this.lastOk = this.now(); this._lostFired = false
    this._timer = this._si(() => { this.tick().catch(() => {}) }, Math.floor(this.ttlMs / 3))
    this._timer.unref?.()
  }

  // One heartbeat. Lost = another owner, or no successful renew within ttl/2: well before the key can expire server-side,
  // even allowing for event-loop lag. The deadline is measured from when the renew was SENT, not when it returned.
  async tick() {
    if (!this.held) return
    const sentAt = this.now()
    let r
    try { r = await this.backend.renew(this.key, this.token, this.ttlMs) } catch {
      if (this.now() - this.lastOk >= this.ttlMs / 2) this._lose('renew failing')
      return
    }
    if (r === 'lost') return this._lose('taken over')
    // The key had vanished and we re-created it: if that was long ago someone else may have held it in between.
    if (r === 'reacquired' && this.now() - this.lastOk >= this.ttlMs / 2) return this._lose('lapsed')
    this.lastOk = sentAt
  }

  // Immediate proof of ownership. `strict` demands the key was still ours ('ok'): a key that had to be re-created
  // (evicted, or taken and evicted again) proves nothing about the past, so it must not authorise wiping credentials.
  async verify({ strict = false } = {}) {
    if (!this.held) return false
    try {
      const sentAt = this.now()
      const r = await this.backend.renew(this.key, this.token, this.ttlMs)
      if (r === 'lost') { this._lose('taken over'); return false }
      this.lastOk = sentAt
      return strict ? r === 'ok' : true
    } catch { return false }
  }

  _lose(why) {
    if (this._lostFired) return
    this._lostFired = true; this.held = false
    if (this._timer) { this._ci(this._timer); this._timer = null }
    this.log?.warn({ session: this.sessionKey, why }, 'lease lost')
    this.onLost?.(why)
  }

  async release() {
    const wasHeld = this.held
    this.held = false; this._lostFired = true
    if (this._timer) { this._ci(this._timer); this._timer = null }
    if (wasHeld) { try { await this.backend.release(this.key, this.token) } catch { /* TTL will expire it */ } }
  }
}
