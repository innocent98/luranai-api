import test from 'node:test'
import assert from 'node:assert/strict'
import { openAuthState } from '../src/auth-state.mjs'
import { deriveSessionKey, open, seal } from '../src/crypto.mjs'
import { MemoryAuthDb } from '../src/db.mjs'
import { MASTER, tick } from './helpers.mjs'

const mk = async (db, sessionKey = 'tenant-a', over = {}) => {
  if (!(await db.getSession(sessionKey))) await db.createSession(sessionKey, Buffer.from('x'), 'connecting')
  return openAuthState({ db, sessionKey, masterHex: MASTER, flushMs: 20, ...over })
}

test('creds and keys round trip through the encrypted store (Buffers survive)', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db)
  a.state.creds.advSecretKey = 'marker-creds-secret'
  await a.saveCreds()
  await a.state.keys.set({ 'pre-key': { 1: { private: Buffer.from('priv-1'), public: Buffer.from('pub-1') } }, session: { 'x.0': Buffer.from('sess') } })
  await a.close()
  const b = await mk(db)
  assert.equal(b.state.creds.advSecretKey, 'marker-creds-secret')
  const got = await b.state.keys.get('pre-key', ['1', '2'])
  assert.deepEqual(Buffer.from(got['1'].private), Buffer.from('priv-1'))
  assert.equal(got['2'], null)
  assert.deepEqual(Buffer.from((await b.state.keys.get('session', ['x.0']))['x.0']), Buffer.from('sess'))
})

test('stored rows are ciphertext: no plaintext, no key material in the database', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db)
  a.state.creds.advSecretKey = 'PLAINTEXT-MARKER'
  await a.saveCreds(); await a.state.keys.set({ session: { s: Buffer.from('PLAINTEXT-MARKER-2') } }); await a.close()
  for (const blob of db.blobs.values()) {
    assert.ok(!blob.includes('PLAINTEXT-MARKER'))
    assert.ok(!blob.toString('base64').includes(Buffer.from('PLAINTEXT-MARKER').toString('base64').slice(0, 12)))
  }
  assert.ok(db.blobCount('tenant-a') >= 2)
})

test('ciphertext is bound to its session and slot: a copied row fails authentication', async () => {
  const k1 = deriveSessionKey(MASTER, 'tenant-a'), k2 = deriveSessionKey(MASTER, 'tenant-b')
  assert.notDeepEqual(k1, k2)
  const blob = seal(k1, '{"x":1}', 'tenant-a', 'session', 'id1')
  assert.equal(open(k1, blob, 'tenant-a', 'session', 'id1'), '{"x":1}')
  assert.throws(() => open(k1, blob, 'tenant-a', 'session', 'id2'))      // other slot
  assert.throws(() => open(k1, blob, 'tenant-b', 'session', 'id1'))      // other session, same key
  assert.throws(() => open(k2, blob, 'tenant-b', 'session', 'id1'))      // other session, its own key
  const bad = Buffer.from(blob); bad[bad.length - 1] ^= 1
  assert.throws(() => open(k1, bad, 'tenant-a', 'session', 'id1'))       // tamper
})

test('a different master key cannot read the credentials (store refuses to open)', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db); await a.saveCreds(); await a.close()
  await assert.rejects(openAuthState({ db, sessionKey: 'tenant-a', masterHex: 'cd'.repeat(32) }))
})

test('writes are batched: many key updates become one database round trip', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db, 'tenant-a', { flushMs: 30 })
  const data = {}
  for (let i = 0; i < 100; i++) data[`k${i}`] = Buffer.from(`v${i}`)
  await a.state.keys.set({ 'pre-key': Object.fromEntries(Object.entries(data).map(([k, v]) => [k, { private: v, public: v }])) })
  await a.state.keys.set({ session: { s1: Buffer.from('1'), s2: Buffer.from('2') } })
  assert.equal(db.putCalls, 0)
  await tick(80)
  assert.equal(db.putCalls, 1)
  assert.equal(db.blobCount('tenant-a'), 102)
  await a.close()
})

test('reads see pending writes before they are flushed, and deletes', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db, 'tenant-a', { flushMs: 10_000 })
  await a.state.keys.set({ session: { s: Buffer.from('v') } })
  assert.equal(db.putCalls, 0)
  assert.deepEqual(Buffer.from((await a.state.keys.get('session', ['s'])).s), Buffer.from('v'))
  await a.state.keys.set({ session: { s: null } })
  assert.equal((await a.state.keys.get('session', ['s'])).s, null)
  await a.close()
  assert.equal(db.blobCount('tenant-a'), 0)
})

test('a failed flush keeps the data and retries', async () => {
  const db = new MemoryAuthDb()
  const real = db.putBlobs.bind(db); let fail = true
  db.putBlobs = async (...a) => { if (fail) { fail = false; throw new Error('db down') } return real(...a) }
  const a = await mk(db, 'tenant-a', { flushMs: 10 })
  await a.state.keys.set({ session: { s: Buffer.from('v') } })
  await assert.rejects(a.flush())
  assert.equal(a.pendingCount(), 1)
  await a.flush()
  assert.equal(db.blobCount('tenant-a'), 1)
  await a.close()
})

test('wipe removes every row, and a late write cannot resurrect them', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db)
  await a.saveCreds(); await a.state.keys.set({ session: { s: Buffer.from('v') } }); await a.flush()
  assert.ok(db.blobCount('tenant-a') >= 2)
  await db.touchChat('tenant-a', 'h', 1)
  await a.wipe()
  assert.equal(db.blobCount('tenant-a'), 0)
  assert.equal(await db.chatAt('tenant-a', 'h'), null)
  assert.equal((await db.getSession('tenant-a')).registered, false)
  await a.state.keys.set({ session: { late: Buffer.from('x') } }); await a.saveCreds(); await a.flush().catch(() => {}); await tick(50)
  assert.equal(db.blobCount('tenant-a'), 0)
})

test('abandon (lease lost) drops pending writes without flushing them', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db, 'tenant-a', { flushMs: 10_000 })
  await a.state.keys.set({ session: { s: Buffer.from('v') } })
  a.abandon(); await a.close().catch(() => {})
  assert.equal(db.blobCount('tenant-a'), 0)
})

test('two sessions never read each other\'s keys', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db, 'tenant-a'), b = await mk(db, 'tenant-b')
  await a.state.keys.set({ session: { s: Buffer.from('A') } }); await a.close()
  assert.equal((await b.state.keys.get('session', ['s'])).s, null)
})

test('saveCreds flushes within ~200 ms even when a slower flush timer is already armed', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db, 'tenant-a', { flushMs: 10_000 })
  await a.state.keys.set({ session: { s: Buffer.from('v') } })
  await a.saveCreds()
  await tick(400)
  assert.equal(db.blobCount('tenant-a'), 2)
  await a.close()
})

test('close() flushes through the same queue and then refuses further writes', async () => {
  const db = new MemoryAuthDb()
  const a = await mk(db, 'tenant-a', { flushMs: 10_000 })
  await a.state.keys.set({ session: { s: Buffer.from('v') } })
  await a.close()
  assert.equal(db.blobCount('tenant-a'), 1)
  await a.state.keys.set({ session: { t: Buffer.from('w') } }); await a.saveCreds()
  assert.equal(a.pendingCount(), 0)
})
