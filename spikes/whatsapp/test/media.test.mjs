import test from 'node:test'
import assert from 'node:assert/strict'
import { Readable } from 'node:stream'
import { MediaTooLarge, declaredLength, readCapped } from '../src/media.mjs'

test('declaredLength fails closed on missing, NaN, negative; reads Long-like and numbers', () => {
  assert.equal(declaredLength(undefined), Infinity)
  assert.equal(declaredLength(null), Infinity)
  assert.equal(declaredLength('abc'), Infinity)
  assert.equal(declaredLength(-5), Infinity)
  assert.equal(declaredLength(1234), 1234)
  assert.equal(declaredLength({ toNumber: () => 99 }), 99)
})
test('readCapped returns bytes under the cap', async () => {
  const b = await readCapped(Readable.from([Buffer.alloc(10), Buffer.alloc(5)]), 100)
  assert.equal(b.length, 15)
})
test('readCapped aborts when received bytes exceed the cap, whatever was declared', async () => {
  const s = Readable.from([Buffer.alloc(60), Buffer.alloc(60)])
  await assert.rejects(readCapped(s, 100), MediaTooLarge)
  assert.equal(s.destroyed, true)
})
