import test from 'node:test'
import assert from 'node:assert/strict'
import { sign, verify } from '../src/sign.mjs'

const S = 'secret-one-secret-one-secret-one-xx'
const P = 'secret-old-secret-old-secret-old-xx'
const body = '{"id":"d1","type":"session.status"}'

test('signature format matches the spike receiver byte for byte (pinned vector)', () => {
  // Vector produced by spikes/whatsapp/src/sign.mjs; the backend receiver was written against that format.
  assert.equal(sign('k'.repeat(32), '{"a":1}', 1700000000), 't=1700000000,v1=6935b796c0fec66e5dee52c86c7fd1218a424c32f1adb239ff2893c31e54f033')
  assert.match(sign(S, body, 1700000000), /^t=1700000000,v1=[0-9a-f]{64}$/)
})

test('valid signature verifies', () => {
  const now = 1700000100
  assert.ok(verify(S, body, sign(S, body, now), now))
})

test('tampered body, tampered mac and wrong secret are rejected', () => {
  const now = 1700000100, h = sign(S, body, now)
  assert.ok(!verify(S, body + ' ', h, now))
  assert.ok(!verify(S, body, h.slice(0, -1) + (h.endsWith('0') ? '1' : '0'), now))
  assert.ok(!verify('x'.repeat(40), body, h, now))
})

test('stale and far-future timestamps are rejected (5 minute window)', () => {
  const t = 1700000000
  assert.ok(verify(S, body, sign(S, body, t), t + 299))
  assert.ok(!verify(S, body, sign(S, body, t), t + 301))
  assert.ok(!verify(S, body, sign(S, body, t + 400), t))
})

test('rotation: previous secret is accepted while listed, rejected once dropped', () => {
  const now = 1700000100, h = sign(P, body, now)
  assert.ok(verify([S, P], body, h, now))
  assert.ok(!verify([S], body, h, now))
  assert.ok(verify([S, null, P], body, sign(S, body, now), now))
})

test('garbage headers and empty secrets are rejected', () => {
  for (const h of [undefined, '', 'nope', 't=abc,v1=00', 't=1,v1=' + 'g'.repeat(64)]) assert.ok(!verify(S, body, h, 1))
  assert.ok(!verify([], body, sign(S, body, 5), 5))
  assert.ok(!verify([null, ''], body, sign(S, body, 5), 5))
})
