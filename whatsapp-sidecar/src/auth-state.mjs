// Encrypted, batched replacement for Baileys' useMultiFileAuthState, backed by an AuthDb.
// Writes are write-behind: key updates are visible immediately (pending map + LRU) and flushed in one transaction
// every `flushMs` (the spike's file store wrote 7,661 files in 10 minutes). After wipe()/close() nothing can write again.
import { errName } from './log.mjs'
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys'
import { deriveSessionKey, open, seal } from './crypto.mjs'

const K = (kind, id) => `${kind}\0${id}`

export async function openAuthState({ db, sessionKey, masterHex, log, flushMs = 1000, cacheMax = 256, maxPending = 200 }) {
  const key = deriveSessionKey(masterHex, sessionKey)
  const dec = (kind, id, blob) => JSON.parse(open(key, blob, sessionKey, kind, id), BufferJSON.reviver)

  const credsBlob = (await db.getBlobs(sessionKey, 'creds', ['creds'])).get('creds')
  const creds = credsBlob ? dec('creds', 'creds', credsBlob) : initAuthCreds()

  const pending = new Map()   // K -> serialized JSON string | null (delete)
  const cache = new Map()     // K -> serialized JSON string (read LRU, bounded)
  const remember = (k, v) => { cache.delete(k); cache.set(k, v); if (cache.size > cacheMax) cache.delete(cache.keys().next().value) }
  let timer = null, timerDue = 0, closed = false, closing = false, chain = Promise.resolve(), failures = 0

  const schedule = (ms = flushMs) => {
    if (closed || closing) return
    const due = Date.now() + ms
    if (timer && timerDue <= due) return          // an earlier flush is already armed
    if (timer) clearTimeout(timer)
    timerDue = due
    timer = setTimeout(() => { timer = null; flush().catch(() => {}) }, ms)
    timer.unref?.()
  }

  const flushNow = async () => {
    if (!pending.size) return
    const batch = new Map(pending); pending.clear()
    try {
      const rows = [...batch].map(([k, v]) => {
        const [kind, id] = k.split('\0')
        return { kind, id, blob: v === null ? null : seal(key, v, sessionKey, kind, id) }
      })
      await db.putBlobs(sessionKey, rows)
      failures = 0
    } catch (e) {
      for (const [k, v] of batch) if (!pending.has(k)) pending.set(k, v)   // keep newer values, retry the rest
      failures++
      log?.warn({ err: errName(e), pending: pending.size, failures }, 'auth flush failed; will retry')
      schedule(Math.min(30000, 1000 * 2 ** Math.min(failures, 5)))
      throw e
    }
  }
  const flush = () => {
    const run = chain.then(() => (closed ? undefined : flushNow()))
    chain = run.catch(() => {})   // a failed flush must not poison later ones
    return run
  }

  const queue = (k, v) => { if (closing || closed) return; pending.set(k, v); if (pending.size >= maxPending) flush().catch(() => {}); else schedule() }

  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const out = {}, misses = []
          for (const id of ids) {
            const k = K(type, id)
            const v = pending.has(k) ? pending.get(k) : cache.get(k)
            if (v === undefined) misses.push(id)
            else out[id] = v === null ? null : JSON.parse(v, BufferJSON.reviver)
          }
          if (misses.length) {
            const rows = await db.getBlobs(sessionKey, type, misses)
            for (const id of misses) {
              const blob = rows.get(id)
              if (!blob) { out[id] = null; continue }
              const text = open(key, blob, sessionKey, type, id)
              remember(K(type, id), text)
              out[id] = JSON.parse(text, BufferJSON.reviver)
            }
          }
          for (const id of ids) if (type === 'app-state-sync-key' && out[id]) out[id] = proto.Message.AppStateSyncKeyData.fromObject(out[id])
          return out
        },
        set: async (data) => {
          if (closed) return
          for (const type in data) for (const id in data[type]) {
            const val = data[type][id], k = K(type, id)
            if (val) { const s = JSON.stringify(val, BufferJSON.replacer); remember(k, s); queue(k, s) }
            else { cache.delete(k); queue(k, null) }
          }
        },
      },
    },
    saveCreds: async () => { if (closed) return; queue(K('creds', 'creds'), JSON.stringify(creds, BufferJSON.replacer)); schedule(200) },
    flush,
    pendingCount: () => pending.size,
    // Flush then stop accepting writes (SIGTERM, lease loss, deliberate stop). Credentials stay in the database.
    close: async () => {
      if (timer) { clearTimeout(timer); timer = null }
      closing = true                               // no new writes; the final flush goes through the same chain as every other
      try { await flush() } finally { closed = true }
    },
    // Lease lost: another process may own these credentials now. Drop pending writes WITHOUT flushing so we can
    // never overwrite their newer keys, and stop accepting writes.
    abandon: () => {
      closed = true; closing = true
      if (timer) { clearTimeout(timer); timer = null }
      pending.clear(); cache.clear()
    },
    // Logout: drop pending writes, wait for any in-flight flush, then delete every row for this session.
    wipe: async () => {
      closed = true; closing = true
      if (timer) { clearTimeout(timer); timer = null }
      pending.clear(); cache.clear()
      await chain.catch(() => {})
      await db.wipeAuth(sessionKey)
    },
  }
}
