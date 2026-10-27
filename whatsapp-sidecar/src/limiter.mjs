// Send pacing: per-session token bucket (default 6/min) + a daily cap that survives restarts when a Redis counter is used.
export class TokenBucket {
  constructor({ perMin, now = () => Date.now() }) {
    this.cap = perMin; this.tokens = perMin; this.rate = perMin / 60000; this.now = now; this.last = now()
  }
  tryTake() {
    const t = this.now()
    this.tokens = Math.min(this.cap, this.tokens + (t - this.last) * this.rate); this.last = t
    if (this.tokens < 1) return false
    this.tokens -= 1; return true
  }
}

export class MemoryCounter {
  constructor() { this.m = new Map() }
  async incr(key) { const n = (this.m.get(key) || 0) + 1; this.m.set(key, n); return n }
}
export class RedisCounter {
  constructor(redis) { this.redis = redis }
  async incr(key, ttlS) {
    const n = await this.redis.incr(key)
    if (n === 1) await this.redis.expire(key, ttlS)
    return n
  }
}

export class DailyCap {
  constructor({ counter, cap, now = () => Date.now() }) { Object.assign(this, { counter, cap, now }) }
  async tryTake(sessionKey) {
    const day = new Date(this.now()).toISOString().slice(0, 10).replaceAll('-', '')
    let n
    try { n = await this.counter.incr(`wa:daily:${sessionKey}:${day}`, 26 * 3600) } catch { return false }   // cannot count: refuse (fail closed)
    return n <= this.cap
  }
}
