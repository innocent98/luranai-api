// Real Postgres + Redis. Skipped unless TEST_DATABASE_URL and TEST_REDIS_URL are set (CI sets them from service containers).
import test from 'node:test'
import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import Redis from 'ioredis'
import { openAuthState } from '../src/auth-state.mjs'
import { PgAuthDb, SCHEMA } from '../src/db.mjs'
import { Lease, LeaseHeldElsewhere, LeaseUnavailable, RedisLeaseBackend } from '../src/lease.mjs'
import { RedisCounter } from '../src/limiter.mjs'
import { MASTER, makeHarness, pairAndConnect, textMsg, tick, until } from './helpers.mjs'

const DB = process.env.TEST_DATABASE_URL, REDIS = process.env.TEST_REDIS_URL
const skip = !DB || !REDIS ? 'set TEST_DATABASE_URL and TEST_REDIS_URL' : false
const ready = async (r) => { if (r.status !== 'ready') await new Promise((res) => r.once('ready', res)); return r }
const uid = () => `it-${randomBytes(4).toString('hex')}`

test('integration: schema init is idempotent and safe under concurrent boots', { skip }, async () => {
  const dbs = [new PgAuthDb(DB), new PgAuthDb(DB), new PgAuthDb(DB)]
  await Promise.all(dbs.map((d) => d.init()))
  await dbs[0].init()
  const r = await dbs[0].pool.query(`SELECT table_name FROM information_schema.tables WHERE table_schema=$1 ORDER BY 1`, [SCHEMA])
  assert.deepEqual(r.rows.map((x) => x.table_name), ['auth_blobs', 'inbound_chats', 'sessions'])
  await Promise.all(dbs.map((d) => d.close()))
})

test('integration: Postgres store contract (upsert, delete, batch, wipe, cascade, chats)', { skip }, async () => {
  const db = new PgAuthDb(DB); await db.init()
  const k = uid(), other = uid()
  assert.equal(await db.createSession(k, Buffer.from('p'), 'connecting'), true)
  assert.equal(await db.createSession(k, Buffer.from('p'), 'connecting'), false)
  await db.createSession(other, Buffer.from('p'), 'connecting')
  await db.putBlobs(k, [{ kind: 'session', id: 'a', blob: Buffer.from('1') }, { kind: 'session', id: 'b', blob: Buffer.from('2') }, { kind: 'creds', id: 'creds', blob: Buffer.from('c') }])
  await db.putBlobs(other, [{ kind: 'session', id: 'a', blob: Buffer.from('other') }])
  await db.putBlobs(k, [{ kind: 'session', id: 'a', blob: Buffer.from('1b') }, { kind: 'session', id: 'b', blob: null }])
  const got = await db.getBlobs(k, 'session', ['a', 'b', 'zz'])
  assert.deepEqual([...got.keys()], ['a']); assert.equal(got.get('a').toString(), '1b')
  await db.touchChat(k, 'hash1', 1_800_000_000_000)
  assert.equal(await db.chatAt(k, 'hash1'), 1_800_000_000_000)
  await db.updateSession(k, { registered: true, status: 'connected', lastDisconnect: { code: 428, at: new Date().toISOString() } })
  const row = await db.getSession(k)
  assert.equal(row.registered, true); assert.equal(row.last_disconnect_code, 428)
  await db.wipeAuth(k)
  assert.equal((await db.getBlobs(k, 'creds', ['creds'])).size, 0)
  assert.equal(await db.chatAt(k, 'hash1'), null)
  assert.equal((await db.getSession(k)).registered, false)
  assert.equal((await db.getBlobs(other, 'session', ['a'])).get('a').toString(), 'other')   // other session untouched
  await db.deleteSession(other)
  assert.equal((await db.pool.query(`SELECT 1 FROM ${SCHEMA}.auth_blobs WHERE session_key=$1`, [other])).rowCount, 0)   // cascade
  await db.deleteSession(k); await db.close()
})

test('integration: encrypted batched auth state on Postgres; raw rows hold no plaintext; 500 key writes = few transactions', { skip }, async () => {
  const db = new PgAuthDb(DB); await db.init()
  const k = uid(); await db.createSession(k, Buffer.from('p'), 'connecting')
  const a = await openAuthState({ db, sessionKey: k, masterHex: MASTER, flushMs: 50 })
  a.state.creds.advSecretKey = 'PLAINTEXT-MARKER'
  await a.saveCreds()
  const keys = {}
  for (let i = 0; i < 500; i++) keys[`k${i}`] = { private: Buffer.from(`PLAINTEXT-MARKER-${i}`), public: Buffer.from('pub') }
  let queries = 0; const real = db.pool.connect.bind(db.pool)
  db.pool.connect = (...args) => { queries++; return real(...args) }
  await a.state.keys.set({ 'pre-key': keys })
  await a.close()
  assert.ok(queries <= 4, `expected few transactions, saw ${queries}`)
  const raw = await db.pool.query(`SELECT ciphertext FROM ${SCHEMA}.auth_blobs WHERE session_key=$1`, [k])
  assert.equal(raw.rowCount, 501)
  for (const r of raw.rows) assert.ok(!r.ciphertext.includes('PLAINTEXT-MARKER'))
  db.pool.connect = real
  const b = await openAuthState({ db, sessionKey: k, masterHex: MASTER })
  assert.equal(b.state.creds.advSecretKey, 'PLAINTEXT-MARKER')
  assert.equal(Buffer.from((await b.state.keys.get('pre-key', ['k499']))['k499'].private).toString(), 'PLAINTEXT-MARKER-499')
  await b.wipe()
  assert.equal((await db.pool.query(`SELECT 1 FROM ${SCHEMA}.auth_blobs WHERE session_key=$1`, [k])).rowCount, 0)
  await db.deleteSession(k); await db.close()
})

test('integration: Redis lease (exclusive, renew, takeover, eviction, expiry, fail closed)', { skip }, async () => {
  const redis = await ready(new Redis(REDIS, { enableOfflineQueue: false, maxRetriesPerRequest: 1 }))
  const be = new RedisLeaseBackend(redis)
  const k = uid(), mk = (ttl = 600, onLost) => new Lease({ backend: be, sessionKey: k, ttlMs: ttl, onLost })
  const a = mk(), b = mk()
  await a.acquire()
  await assert.rejects(b.acquire(), LeaseHeldElsewhere)
  assert.equal(await a.verify(), true)
  const key = a.key
  await redis.del(key)                                   // eviction: the owner re-creates its own key
  assert.equal(await a.verify(), true)
  await assert.rejects(b.acquire(), LeaseHeldElsewhere)
  await b.release()                                      // non-owner release is a no-op
  await assert.rejects(mk().acquire(), LeaseHeldElsewhere)
  await redis.set(key, 'intruder', 'PX', 5000)           // takeover
  assert.equal(await a.verify(), false)
  await redis.del(key)
  let lost = 0
  const c = mk(600, () => lost++); await c.acquire()     // heartbeat keeps it alive past the ttl
  await tick(1000); assert.equal(lost, 0); assert.equal(await redis.pexpire(key, 600) , 1)
  await c.release(); assert.equal(await redis.exists(key), 0)
  const dead = new Redis('redis://127.0.0.1:1', { enableOfflineQueue: false, maxRetriesPerRequest: 0, retryStrategy: () => null, lazyConnect: true })
  dead.on('error', () => {})
  await assert.rejects(new Lease({ backend: new RedisLeaseBackend(dead), sessionKey: k, ttlMs: 600 }).acquire(), LeaseUnavailable)
  await redis.quit(); dead.disconnect()
})

test('integration: Redis daily counter', { skip }, async () => {
  const redis = await ready(new Redis(REDIS)), c = new RedisCounter(redis), k = `wa:daily:${uid()}`
  assert.deepEqual([await c.incr(k, 60), await c.incr(k, 60), await c.incr(k, 60)], [1, 2, 3])
  assert.ok((await redis.ttl(k)) > 0)
  await redis.del(k); await redis.quit()
})

test('integration: full flow on real Postgres + Redis (fake socket): pair, message, restart restore, second process blocked by lease, logout wipes', { skip }, async () => {
  const redis = await ready(new Redis(REDIS, { enableOfflineQueue: false, maxRetriesPerRequest: 1 }))
  const db = new PgAuthDb(DB); await db.init()
  const key = uid(), CUST = '2348012345678@s.whatsapp.net'
  const mkH = (extra = {}) => makeHarness({ db, leaseBackend: new RedisLeaseBackend(redis), counter: new RedisCounter(redis), ...extra })
  const h1 = mkH()
  await pairAndConnect(h1, key, '2348000000001')
  h1.last().upsert([textMsg('IN1', CUST, 'hi')]); await until(() => h1.events('message.inbound').length === 1)
  assert.ok((await h1.mgr.send(key, CUST, 'hello')).message_id)
  assert.ok((await db.pool.query(`SELECT 1 FROM ${SCHEMA}.auth_blobs WHERE session_key=$1`, [key])).rowCount >= 1)
  await tick(30)

  const h2 = mkH(); await h2.mgr.restoreAll(); await tick(60)       // second process, first still holds the lease
  assert.equal(h2.sockets.length, 0); assert.equal(h2.mgr.get(key).status, 'reconnecting')
  await h2.mgr.shutdown()

  await h1.mgr.shutdown()                                           // clean stop releases the lease
  const h3 = mkH(); await h3.mgr.restoreAll(); await h3.mgr.restoring
  assert.equal(h3.sockets.length, 1)
  h3.last().open(); await until(() => h3.mgr.get(key).status === 'connected')
  assert.ok((await h3.mgr.send(key, CUST, 'after restart')).message_id)         // reply-only state came back from Postgres

  await h3.mgr.remove(key)
  assert.equal((await db.pool.query(`SELECT 1 FROM ${SCHEMA}.auth_blobs WHERE session_key=$1`, [key])).rowCount, 0)
  assert.equal(await redis.exists(`wa:lease:${key}`), 0)
  await db.deleteSession(key); await db.close(); await redis.quit()
})
