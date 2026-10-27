import test from 'node:test'
import assert from 'node:assert/strict'
import { sign, verify } from '../src/sign.mjs'

const S = 'unit-test-secret-not-real'
test('valid signature verifies', () => {
  const h = sign(S, '{"a":1}', 1000)
  assert.equal(verify(S, '{"a":1}', h, 1010), true)
})
test('tampered body, wrong secret, stale ts, garbage header are rejected', () => {
  const h = sign(S, '{"a":1}', 1000)
  assert.equal(verify(S, '{"a":2}', h, 1010), false)
  assert.equal(verify('other', '{"a":1}', h, 1010), false)
  assert.equal(verify(S, '{"a":1}', h, 1000 + 301), false)
  assert.equal(verify(S, '{"a":1}', 'nope', 1010), false)
  assert.equal(verify(S, '{"a":1}', undefined, 1010), false)
})
