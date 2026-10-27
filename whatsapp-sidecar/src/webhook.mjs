// Signed webhook delivery sidecar -> backend. In-order per session, retries with exponential backoff + jitter,
// stable delivery id (receiver dedupes on it), fresh signature timestamp per attempt (the receiver rejects > 5 min old).
// Never logs bodies: they contain message text and identifiers.
import { randomUUID } from 'node:crypto'
import { SIGNATURE_HEADER, sign } from './sign.mjs'

const PERMANENT = new Set([400, 413, 422])   // the receiver says this body will never be accepted: do not retry

export class WebhookDelivery {
  constructor({ url, secret, log, fetchImpl = fetch, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), random = Math.random,
    maxAttempts = 8, baseMs = 1000, capMs = 60000, perSessionMax = 1000, timeoutMs = 10000, now = () => new Date() }) {
    Object.assign(this, { url, secret, log, fetchImpl, sleep, random, maxAttempts, baseMs, capMs, perSessionMax, timeoutMs, now })
    this.queues = new Map()   // session_key -> { items: [], running: bool }
    this.stats = { delivered: 0, retried: 0, dropped: 0 }
  }

  emit(sessionKey, event) {
    const body = JSON.stringify({ id: randomUUID(), at: this.now().toISOString(), session_key: sessionKey, ...event })
    let q = this.queues.get(sessionKey)
    if (!q) this.queues.set(sessionKey, (q = { items: [], running: false }))
    if (q.items.length >= this.perSessionMax) {
      this.stats.dropped++
      this.log?.error({ type: event.type, session: sessionKey }, 'webhook queue full; event dropped')
      return null
    }
    q.items.push({ body, type: event.type })
    if (!q.running) this._run(sessionKey, q)
    return body
  }

  async _run(sessionKey, q) {
    q.running = true
    try {
      while (q.items.length) {
        const item = q.items[0]
        await this._deliver(sessionKey, item)
        q.items.shift()
      }
    } finally { q.running = false; if (!q.items.length) this.queues.delete(sessionKey) }
  }

  async _deliver(sessionKey, item) {
    for (let attempt = 0; attempt < this.maxAttempts; attempt++) {
      let status = 0
      try {
        const res = await this.fetchImpl(this.url, {
          method: 'POST', body: item.body, redirect: 'error', signal: AbortSignal.timeout(this.timeoutMs),
          headers: { 'content-type': 'application/json', [SIGNATURE_HEADER]: sign(this.secret, item.body) },
        })
        status = res.status
        await res.arrayBuffer?.().catch(() => {})
        if (res.ok) { this.stats.delivered++; return }
        if (PERMANENT.has(status)) { this.stats.dropped++; this.log?.error({ type: item.type, session: sessionKey, status }, 'webhook rejected permanently'); return }
      } catch { /* network error or timeout: retry */ }
      if (attempt + 1 >= this.maxAttempts) break
      this.stats.retried++
      const delay = Math.min(this.capMs, this.baseMs * 2 ** attempt) * (0.5 + this.random() * 0.5)
      this.log?.warn({ type: item.type, session: sessionKey, attempt: attempt + 1, status }, 'webhook delivery failed; retrying')
      await this.sleep(delay)
    }
    this.stats.dropped++
    this.log?.error({ type: item.type, session: sessionKey }, 'webhook delivery abandoned after retries')
  }

  pending() { let n = 0; for (const q of this.queues.values()) n += q.items.length; return n }

  // Wait (bounded) for in-flight deliveries; used on shutdown.
  async drain(timeoutMs = 5000) {
    const end = Date.now() + timeoutMs
    while (this.pending() && Date.now() < end) await new Promise((r) => setTimeout(r, 50))
    return this.pending() === 0
  }
}
