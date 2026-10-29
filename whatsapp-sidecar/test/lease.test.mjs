import test from 'node:test'
import assert from 'node:assert/strict'
import { Lease, LeaseHeldElsewhere, LeaseUnavailable, MemoryLeaseBackend, leaseKey } from '../src/lease.mjs'

const mk = (backend, clock, over = {}) => {
  const lost = []
  const lease = new Lease({ backend, sessionKey: over.key || 's1', ttlMs: 30000, now: () => clock.now, onLost: (w) => lost.push(w),
    setInterval: () => ({ unref() {} }), clearInterval: () => {} })
  return { lease, lost }
}

test('exclusive acquire, release by owner only, then reacquirable', async () => {
  const clock = { now: 1000 }, be = new MemoryLeaseBackend(() => clock.now)
  const a = mk(be, clock).lease, b = mk(be, clock).lease
  await a.acquire()
  await assert.rejects(b.acquire(), LeaseHeldElsewhere)
  await b.release()                                   // not the owner: must not free A's lease
  await assert.rejects(mk(be, clock).lease.acquire(), LeaseHeldElsewhere)
  await a.release()
  await mk(be, clock).lease.acquire()
  assert.ok(leaseKey('s1').startsWith('wa:lease:'))
})

test('heartbeat extends the lease; an expired lease can be taken', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease } = mk(be, clock)
  await lease.acquire()
  clock.now = 20000; await lease.tick()
  clock.now = 45000                                    // 25 s after the renew: still inside the 30 s ttl
  await assert.rejects(mk(be, clock).lease.acquire(), LeaseHeldElsewhere)
  clock.now = 60000
  await mk(be, clock).lease.acquire()
})

test('takeover by another owner is detected on the next heartbeat and fires onLost once', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease, lost } = mk(be, clock)
  await lease.acquire()
  be.steal(lease.key, 'other', 30000)
  await lease.tick(); await lease.tick()
  assert.equal(lost.length, 1)
  assert.equal(lease.held, false)
  assert.equal(await lease.verify(), false)
})

test('fail closed: Redis down at acquire throws LeaseUnavailable', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  be.down = true
  const { lease } = mk(be, clock)
  await assert.rejects(lease.acquire(), LeaseUnavailable)
  assert.equal(lease.held, false)
})

test('fail closed: renewals failing past the safety margin stop the session; short blips do not', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease, lost } = mk(be, clock)
  await lease.acquire()
  be.down = true
  clock.now = 10000; await lease.tick()                // blip: 10 s without a renew, margin is 20 s
  assert.equal(lost.length, 0)
  clock.now = 21000; await lease.tick()                // would expire at 30 s: stop before another writer can start
  assert.equal(lost.length, 1)
  assert.equal(await lease.verify(), false)
})

test('an evicted key is re-acquired (allkeys-lru Redis), not treated as ownership by someone else', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease, lost } = mk(be, clock)
  await lease.acquire()
  be.evict(lease.key)
  await lease.tick()
  assert.equal(lost.length, 0)
  assert.equal(await lease.verify(), true)
  await assert.rejects(mk(be, clock).lease.acquire(), LeaseHeldElsewhere)
})

test('verify is false when the backend is unreachable', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease } = mk(be, clock)
  await lease.acquire(); be.down = true
  assert.equal(await lease.verify(), false)
})

test('strict verify: a re-created key is not proof of ownership; the next strict verify is', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease } = mk(be, clock)
  await lease.acquire()
  be.evict(lease.key)
  assert.equal(await lease.verify({ strict: true }), false)
  assert.equal(await lease.verify({ strict: true }), true)
})

test('a key that vanished for longer than ttl/2 and had to be re-created counts as lost', async () => {
  const clock = { now: 0 }, be = new MemoryLeaseBackend(() => clock.now)
  const { lease, lost } = mk(be, clock)
  await lease.acquire()
  be.evict(lease.key); clock.now = 16000
  await lease.tick()
  assert.equal(lost.length, 1)
})
