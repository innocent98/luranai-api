import test from 'node:test'
import assert from 'node:assert/strict'
import { verify } from '../src/sign.mjs'
import { WebhookDelivery } from '../src/webhook.mjs'
import { SECRET, makeLogCapture, until } from './helpers.mjs'

const mk = (responses, over = {}) => {
  const calls = [], logs = makeLogCapture(), sleeps = []
  const fetchImpl = async (url, init) => {
    calls.push({ url, init, at: Math.floor(Date.now() / 1000) })
    const r = responses.shift() ?? 200
    if (r === 'throw') throw new Error('ECONNREFUSED')
    return { status: r, ok: r >= 200 && r < 300, arrayBuffer: async () => new ArrayBuffer(0) }
  }
  const w = new WebhookDelivery({ url: 'http://api/hook', secret: SECRET, log: logs.log, fetchImpl, sleep: async (ms) => { sleeps.push(ms) }, random: () => 0.5, ...over })
  return { w, calls, logs, sleeps }
}

test('delivers a signed body in the contract shape', async () => {
  const { w, calls } = mk([200])
  w.emit('tenant-a', { type: 'session.status', status: 'connected' })
  await until(() => calls.length === 1)
  const { init } = calls[0]
  const body = JSON.parse(init.body)
  assert.match(body.id, /^[0-9a-f-]{36}$/)
  assert.ok(!Number.isNaN(Date.parse(body.at)))
  assert.equal(body.session_key, 'tenant-a'); assert.equal(body.type, 'session.status'); assert.equal(body.status, 'connected')
  assert.ok(verify(SECRET, init.body, init.headers['x-luran-signature']))
  assert.ok(verify(['x'.repeat(40), SECRET], init.body, init.headers['x-luran-signature']))   // receiver mid-rotation
  assert.equal(init.redirect, 'error')
})

test('retries with exponential backoff, same delivery id, fresh signature each attempt', async () => {
  const { w, calls, sleeps } = mk([500, 'throw', 200])
  w.emit('s', { type: 'message.inbound', text: 'hello' })
  await until(() => calls.length === 3)
  assert.deepEqual(calls.map((c) => JSON.parse(c.init.body).id).filter((v, i, a) => a.indexOf(v) === i).length, 1)
  assert.equal(sleeps.length, 2)
  assert.ok(sleeps[1] > sleeps[0])
  assert.equal(w.stats.retried, 2)
  for (const c of calls) assert.ok(verify(SECRET, c.init.body, c.init.headers['x-luran-signature']))
})

test('gives up after maxAttempts and drops, without blocking later events', async () => {
  const { w, calls } = mk(['throw', 'throw', 'throw', 200], { maxAttempts: 3 })
  w.emit('s', { type: 'a' }); w.emit('s', { type: 'b' })
  await until(() => calls.length === 4)
  assert.equal(w.stats.dropped, 1)
  assert.equal(JSON.parse(calls[3].init.body).type, 'b')
})

test('permanent rejection (422) is not retried', async () => {
  const { w, calls } = mk([422, 200])
  w.emit('s', { type: 'a' }); w.emit('s', { type: 'b' })
  await until(() => calls.length === 2)
  assert.equal(JSON.parse(calls[0].init.body).type, 'a'); assert.equal(JSON.parse(calls[1].init.body).type, 'b')
})

test('per-session order is preserved even when an earlier event is retried', async () => {
  const { w, calls } = mk([500, 200, 200])
  w.emit('s', { type: 'first' }); w.emit('s', { type: 'second' })
  await until(() => calls.length === 3)
  assert.deepEqual(calls.map((c) => JSON.parse(c.init.body).type), ['first', 'first', 'second'])
})

test('queue is bounded per session', async () => {
  const { w } = mk(Array(50).fill('throw'), { perSessionMax: 3, maxAttempts: 100, sleep: () => new Promise(() => {}) })
  for (let i = 0; i < 6; i++) w.emit('s', { type: 'x' })
  assert.equal(w.stats.dropped, 3)
})

test('logs never contain message text, phone numbers or ids of the payload', async () => {
  const { w, calls, logs } = mk(['throw', 500, 200])
  w.emit('s', { type: 'message.inbound', text: 'SECRET-CUSTOMER-TEXT', from_ref: '2348012345678@s.whatsapp.net', from_phone_digits: '2348012345678' })
  await until(() => calls.length === 3)
  const out = logs.text()
  assert.ok(out.length > 0)
  assert.ok(!out.includes('SECRET-CUSTOMER-TEXT')); assert.ok(!out.includes('2348012345678'))
})
