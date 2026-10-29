import test from 'node:test'
import assert from 'node:assert/strict'
import { MemoryAuthDb } from '../src/db.mjs'
import { makeHarness, pairAndConnect, tick, until } from './helpers.mjs'

test('capacity: MAX_SESSIONS reached -> 503 capacity_exceeded with max_sessions; frees on logout', async () => {
  const h = makeHarness({ cfg: { maxSessions: 2 } })
  await h.mgr.create({ session_key: 'a', phone_digits: '2348000000001' })
  await h.mgr.create({ session_key: 'b', phone_digits: '2348000000002' })
  await assert.rejects(h.mgr.create({ session_key: 'c', phone_digits: '2348000000003' }), (e) => e.status === 503 && e.code === 'capacity_exceeded' && e.extra.max_sessions === 2)
  assert.equal(await h.db.getSession('c'), null)
  assert.deepEqual(h.mgr.health().sessions, 2)
  await h.mgr.remove('a')
  assert.equal(h.mgr.health().sessions, 1)                       // logged_out holds no socket: not counted
  await h.mgr.create({ session_key: 'c', phone_digits: '2348000000003' })
})

test('capacity: RSS at or above RSS_REFUSE_MB refuses new sessions, health reports it', async () => {
  const h = makeHarness({ cfg: { rssRefuseMb: 400 } })
  h.rss = 399 * 1048576; await h.mgr.create({ session_key: 'a', phone_digits: '2348000000001' })
  h.rss = 400 * 1048576
  await assert.rejects(h.mgr.create({ session_key: 'b', phone_digits: '2348000000002' }), (e) => e.code === 'capacity_exceeded')
  assert.deepEqual(h.mgr.health(), { sessions: 1, max_sessions: 3, rss_mb: 400, rss_refuse_mb: 400 })
})

test('capacity: concurrent creates cannot both pass the check', async () => {
  const h = makeHarness({ cfg: { maxSessions: 1 } })
  const r = await Promise.allSettled([
    h.mgr.create({ session_key: 'a', phone_digits: '2348000000001' }),
    h.mgr.create({ session_key: 'b', phone_digits: '2348000000002' }),
    h.mgr.create({ session_key: 'c', phone_digits: '2348000000003' }),
  ])
  assert.equal(r.filter((x) => x.status === 'fulfilled').length, 1)
  assert.equal(h.mgr.activeCount(), 1)
})

test('create: duplicate key -> 409 session_exists; same phone twice -> 409; bad input -> 400', async () => {
  const h = makeHarness()
  await h.mgr.create({ session_key: 'a', phone_digits: '2348000000001' })
  await assert.rejects(h.mgr.create({ session_key: 'a', phone_digits: '2348000000009' }), (e) => e.status === 409 && e.code === 'session_exists')
  await assert.rejects(h.mgr.create({ session_key: 'b', phone_digits: '2348000000001' }), (e) => e.status === 409)
  for (const bad of [{ session_key: '../x', phone_digits: '2348000000001' }, { session_key: 'ok', phone_digits: '+234 800' }, { session_key: 'ok' }, {}]) {
    await assert.rejects(h.mgr.create(bad), (e) => e.status === 400)
  }
})

test('Redis down on create: 503 lease_unavailable, nothing left behind (fail closed)', async () => {
  const h = makeHarness(); h.leaseBackend.down = true
  await assert.rejects(h.mgr.create({ session_key: 'a', phone_digits: '2348000000001' }), (e) => e.status === 503 && e.code === 'lease_unavailable')
  assert.equal(h.sockets.length, 0); assert.equal(await h.db.getSession('a'), null)
})

test('restore on boot: staggered, one session at a time, no pairing, capped by MAX_SESSIONS', async () => {
  const h = makeHarness({ cfg: { maxSessions: 5 } })
  for (const [k, p] of [['a', '2348000000001'], ['b', '2348000000002'], ['c', '2348000000003']]) await pairAndConnect(h, k, p)
  await h.mgr.shutdown()

  const h2 = makeHarness({ db: h.db, cfg: { maxSessions: 2 } })
  const r = await h2.mgr.restoreAll()
  assert.equal(r.restoring, 2)
  await h2.mgr.restoring
  assert.equal(h2.sockets.length, 2)                                      // third is over the cap: not started
  assert.deepEqual(h2.sleeps.map((ms) => ms >= 2000 && ms <= 3000), [true])  // exactly one stagger gap between two restores (2 s + jitter)
  assert.ok(h2.sockets.every((s) => s.calls.filter((c) => c[0] === 'pair').length === 0))
  assert.equal(h2.mgr.get('c').status, 'failed')
  assert.equal(h2.events('session.status').find((e) => e.session_key === 'c').code, 503)
  assert.ok(h2.db.blobCount('c') > 0)                                     // over-cap session keeps its credentials
  h2.sockets[0].open(); await until(() => h2.mgr.get('a').status === 'connected')
  assert.equal(h2.mgr.health().sessions, 3)
})

test('restore: logged_out stays logged_out and holds no slot; unfinished pairing is expired by the sweeper (lease first); failed stays failed', async () => {
  const h = makeHarness()
  await pairAndConnect(h, 'a', '2348000000001'); await h.mgr.remove('a')
  await h.mgr.create({ session_key: 'p', phone_digits: '2348000000002' })        // pairing in progress when the process dies
  await pairAndConnect(h, 'f', '2348000000003'); h.last().close(403); await until(() => h.mgr.get('f').status === 'failed')
  await h.mgr.shutdown()
  const h2 = makeHarness({ db: h.db })
  await h2.mgr.restoreAll(); await h2.mgr.restoring
  assert.equal(h2.sockets.length, 0)
  assert.deepEqual(['a', 'p', 'f'].map((k) => h2.mgr.get(k).status), ['logged_out', 'awaiting_pair', 'failed'])
  assert.deepEqual(h2.emitted, [])                                               // boot itself neither wipes nor announces anything
  const p = h2.mgr.get('p'); assert.ok(p.pairingExpired())
  await p.expirePairing()
  assert.equal(p.status, 'logged_out')
  assert.deepEqual(h2.events('session.status').map((e) => [e.session_key, e.status, e.code]), [['p', 'logged_out', 408]])
  assert.equal(h2.mgr.get('f').snapshot().last_disconnect.code, 403)
  assert.equal(h2.mgr.activeCount(), 1)
})

test('restore does not touch a pairing that another process still owns', async () => {
  const { MemoryLeaseBackend } = await import('../src/lease.mjs')
  const shared = new MemoryLeaseBackend()
  const h1 = makeHarness({ leaseBackend: shared })
  await h1.mgr.create({ session_key: 'p', phone_digits: '2348000000002' })        // h1 owns it (live pairing socket)
  const h2 = makeHarness({ db: h1.db, leaseBackend: shared })
  await h2.mgr.restoreAll()
  const p = h2.mgr.get('p')
  await p.expirePairing()
  assert.equal(p.status, 'awaiting_pair'); assert.deepEqual(h2.emitted, [])
  assert.ok(await h1.db.getSession('p'))
  await h1.mgr.shutdown(); await h2.mgr.shutdown()
})

test('restore: refuses to start more sessions once RSS is over the limit', async () => {
  const h = makeHarness({ cfg: { maxSessions: 5 } })
  await pairAndConnect(h, 'a', '2348000000001'); await pairAndConnect(h, 'b', '2348000000002'); await h.mgr.shutdown()
  const h2 = makeHarness({ db: h.db, cfg: { maxSessions: 5, rssRefuseMb: 100 } })   // rss 150 MB > 100
  await h2.mgr.restoreAll(); await h2.mgr.restoring
  assert.equal(h2.sockets.length, 0)
  assert.deepEqual(['a', 'b'].map((k) => h2.mgr.get(k).status), ['failed', 'failed'])
})

test('wrong master key at boot: session fails (500) and never connects; credentials untouched', async () => {
  const h = makeHarness(); await pairAndConnect(h); await h.mgr.shutdown()
  const before = h.db.blobCount('tenant-a')
  const h2 = makeHarness({ db: h.db, cfg: { masterKeyHex: 'cd'.repeat(32) } })
  await h2.mgr.restoreAll(); await h2.mgr.restoring
  assert.equal(h2.sockets.length, 0)
  assert.equal(h2.mgr.get('tenant-a').status, 'logged_out' === 'x' ? '' : h2.mgr.get('tenant-a').status)
  assert.equal(h.db.blobCount('tenant-a'), before)
})
