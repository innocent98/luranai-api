// Sidecar-owned Postgres schema (`whatsapp_sidecar`), created idempotently at boot. NOT part of the backend Alembic chain.
// Two implementations share one interface: PgAuthDb (production) and MemoryAuthDb (tests / contract suite).
import pg from 'pg'

export const SCHEMA = 'whatsapp_sidecar'

const DDL = [
  `CREATE SCHEMA IF NOT EXISTS ${SCHEMA}`,
  `CREATE TABLE IF NOT EXISTS ${SCHEMA}.sessions (
     session_key text PRIMARY KEY,
     phone_enc   bytea NOT NULL,
     registered  boolean NOT NULL DEFAULT false,
     status      text NOT NULL,
     last_disconnect_code integer,
     last_disconnect_at   timestamptz,
     created_at  timestamptz NOT NULL DEFAULT now(),
     updated_at  timestamptz NOT NULL DEFAULT now())`,
  `CREATE TABLE IF NOT EXISTS ${SCHEMA}.auth_blobs (
     session_key text NOT NULL REFERENCES ${SCHEMA}.sessions(session_key) ON DELETE CASCADE,
     kind        text NOT NULL,
     id          text NOT NULL,
     ciphertext  bytea NOT NULL,
     updated_at  timestamptz NOT NULL DEFAULT now(),
     PRIMARY KEY (session_key, kind, id))`,
  `CREATE TABLE IF NOT EXISTS ${SCHEMA}.inbound_chats (
     session_key     text NOT NULL REFERENCES ${SCHEMA}.sessions(session_key) ON DELETE CASCADE,
     chat_hash       text NOT NULL,
     last_inbound_at timestamptz NOT NULL,
     PRIMARY KEY (session_key, chat_hash))`,
]

export class PgAuthDb {
  constructor(connectionString, { max = 4 } = {}) {
    this.pool = new pg.Pool({ connectionString, max, idleTimeoutMillis: 30000, connectionTimeoutMillis: 10000 })
    this.pool.on('error', () => { /* idle client error: pool replaces it; callers see query errors */ })
  }

  async init() {
    const c = await this.pool.connect()
    try {
      await c.query('SELECT pg_advisory_lock(7731024)')   // serialise concurrent boots
      // A schema pre-created by the deploy (owned by a least-privilege role without database-level CREATE) must not be re-created:
      // Postgres checks that privilege before honouring IF NOT EXISTS.
      const exists = (await c.query('SELECT 1 FROM pg_namespace WHERE nspname = $1', [SCHEMA])).rowCount > 0
      try { for (const sql of DDL) if (!(exists && sql.startsWith('CREATE SCHEMA'))) await c.query(sql) } finally { await c.query('SELECT pg_advisory_unlock(7731024)') }
    } finally { c.release() }
  }

  async createSession(key, phoneEnc, status) {
    const r = await this.pool.query(
      `INSERT INTO ${SCHEMA}.sessions (session_key, phone_enc, status) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING`, [key, phoneEnc, status])
    return r.rowCount === 1
  }
  async getSession(key) {
    const r = await this.pool.query(`SELECT session_key, phone_enc, registered, status, last_disconnect_code, last_disconnect_at, created_at FROM ${SCHEMA}.sessions WHERE session_key=$1`, [key])
    return r.rows[0] || null
  }
  async listSessions() {
    const r = await this.pool.query(`SELECT session_key, phone_enc, registered, status, last_disconnect_code, last_disconnect_at, created_at FROM ${SCHEMA}.sessions ORDER BY created_at, session_key`)
    return r.rows
  }
  async updateSession(key, { registered, status, lastDisconnect } = {}) {
    await this.pool.query(
      `UPDATE ${SCHEMA}.sessions SET registered = COALESCE($2, registered), status = COALESCE($3, status),
         last_disconnect_code = COALESCE($4, last_disconnect_code), last_disconnect_at = COALESCE($5, last_disconnect_at), updated_at = now()
       WHERE session_key=$1`,
      [key, registered ?? null, status ?? null, lastDisconnect?.code ?? null, lastDisconnect?.at ?? null])
  }
  async deleteSession(key) { await this.pool.query(`DELETE FROM ${SCHEMA}.sessions WHERE session_key=$1`, [key]) }

  async getBlobs(key, kind, ids) {
    const out = new Map()
    if (!ids.length) return out
    const r = await this.pool.query(
      `SELECT id, ciphertext FROM ${SCHEMA}.auth_blobs WHERE session_key=$1 AND kind=$2 AND id = ANY($3::text[])`, [key, kind, ids])
    for (const row of r.rows) out.set(row.id, row.ciphertext)
    return out
  }

  // rows: [{kind, id, blob: Buffer|null}]; null deletes. One transaction, two statements, however many rows.
  async putBlobs(key, rows) {
    if (!rows.length) return
    const up = rows.filter((x) => x.blob), del = rows.filter((x) => !x.blob)
    const c = await this.pool.connect()
    try {
      await c.query('BEGIN')
      if (up.length) {
        await c.query(
          `INSERT INTO ${SCHEMA}.auth_blobs (session_key, kind, id, ciphertext)
           SELECT $1, t.kind, t.id, t.ct FROM unnest($2::text[], $3::text[], $4::bytea[]) AS t(kind, id, ct)
           ON CONFLICT (session_key, kind, id) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()`,
          [key, up.map((x) => x.kind), up.map((x) => x.id), up.map((x) => x.blob)])
      }
      if (del.length) {
        await c.query(
          `DELETE FROM ${SCHEMA}.auth_blobs a USING unnest($2::text[], $3::text[]) AS t(kind, id)
           WHERE a.session_key=$1 AND a.kind=t.kind AND a.id=t.id`,
          [key, del.map((x) => x.kind), del.map((x) => x.id)])
      }
      await c.query('COMMIT')
    } catch (e) { await c.query('ROLLBACK').catch(() => {}); throw e } finally { c.release() }
  }

  async wipeAuth(key) {
    await this.pool.query(`DELETE FROM ${SCHEMA}.auth_blobs WHERE session_key=$1`, [key])
    await this.pool.query(`DELETE FROM ${SCHEMA}.inbound_chats WHERE session_key=$1`, [key])
    await this.updateSession(key, { registered: false })
  }

  async touchChat(key, hash, atMs) {
    await this.pool.query(
      `INSERT INTO ${SCHEMA}.inbound_chats (session_key, chat_hash, last_inbound_at) VALUES ($1,$2,to_timestamp($3/1000.0))
       ON CONFLICT (session_key, chat_hash) DO UPDATE SET last_inbound_at = EXCLUDED.last_inbound_at`, [key, hash, atMs])
  }
  async chatAt(key, hash) {
    const r = await this.pool.query(`SELECT (extract(epoch from last_inbound_at)*1000)::bigint AS ms FROM ${SCHEMA}.inbound_chats WHERE session_key=$1 AND chat_hash=$2`, [key, hash])
    return r.rows[0] ? Number(r.rows[0].ms) : null
  }

  async close() { await this.pool.end() }
}

export class MemoryAuthDb {
  constructor() { this.sessions = new Map(); this.blobs = new Map(); this.chats = new Map(); this.putCalls = 0 }
  async init() {}
  async createSession(key, phoneEnc, status) {
    if (this.sessions.has(key)) return false
    this.sessions.set(key, { session_key: key, phone_enc: phoneEnc, registered: false, status, last_disconnect_code: null, last_disconnect_at: null, created_at: new Date() })
    return true
  }
  async getSession(key) { return this.sessions.get(key) || null }
  async listSessions() { return [...this.sessions.values()].sort((a, b) => a.created_at - b.created_at || a.session_key.localeCompare(b.session_key)) }
  async updateSession(key, { registered, status, lastDisconnect } = {}) {
    const s = this.sessions.get(key); if (!s) return
    if (registered !== undefined) s.registered = registered
    if (status !== undefined) s.status = status
    if (lastDisconnect) { s.last_disconnect_code = lastDisconnect.code; s.last_disconnect_at = new Date(lastDisconnect.at) }
  }
  async deleteSession(key) {
    this.sessions.delete(key)
    for (const k of [...this.blobs.keys()]) if (k.startsWith(`${key}\0`)) this.blobs.delete(k)
    for (const k of [...this.chats.keys()]) if (k.startsWith(`${key}\0`)) this.chats.delete(k)
  }
  async getBlobs(key, kind, ids) {
    const out = new Map()
    for (const id of ids) { const b = this.blobs.get(`${key}\0${kind}\0${id}`); if (b) out.set(id, b) }
    return out
  }
  async putBlobs(key, rows) {
    this.putCalls++
    for (const { kind, id, blob } of rows) {
      const k = `${key}\0${kind}\0${id}`
      if (blob) this.blobs.set(k, Buffer.from(blob)); else this.blobs.delete(k)
    }
  }
  async wipeAuth(key) {
    for (const k of [...this.blobs.keys()]) if (k.startsWith(`${key}\0`)) this.blobs.delete(k)
    for (const k of [...this.chats.keys()]) if (k.startsWith(`${key}\0`)) this.chats.delete(k)
    await this.updateSession(key, { registered: false })
  }
  async touchChat(key, hash, atMs) { this.chats.set(`${key}\0${hash}`, atMs) }
  async chatAt(key, hash) { return this.chats.get(`${key}\0${hash}`) ?? null }
  blobCount(key) { return [...this.blobs.keys()].filter((k) => k.startsWith(`${key}\0`)).length }
  async close() {}
}
