import test from 'node:test'
import assert from 'node:assert/strict'
import { mask } from '../src/log.mjs'
import { BoundedMap, BoundedSet, isOneToOneJid, parseMessage, phoneDigitsOf } from '../src/messages.mjs'

test('only 1:1 jids are accepted', () => {
  for (const j of ['2348012345678@s.whatsapp.net', '123@lid']) assert.ok(isOneToOneJid(j))
  for (const j of ['1203@g.us', 'status@broadcast', '1@newsletter', 'x', '', undefined, '1@s.whatsapp.net.evil.com@g.us']) assert.ok(!isOneToOneJid(j))
})

test('phone digits only from phone jids, device suffix stripped', () => {
  assert.equal(phoneDigitsOf('2348012345678:12@s.whatsapp.net'), '2348012345678')
  assert.equal(phoneDigitsOf('123456@lid'), null)
  assert.equal(phoneDigitsOf('abc@s.whatsapp.net'), null)
})

test('view-once / ephemeral wrappers are unwrapped; text is capped', () => {
  const m = parseMessage({ key: { id: 'A', remoteJid: '1234567@lid' }, message: { ephemeralMessage: { message: { extendedTextMessage: { text: 'x'.repeat(20000) } } } } })
  assert.equal(m.kind, 'text'); assert.equal(m.text.length, 8192)
  assert.equal(parseMessage({ key: { id: 'A', remoteJid: '1234567@lid' }, message: { ephemeralMessage: { message: { protocolMessage: {} } } } }), null)
})

test('mask hides all but the edges', () => {
  assert.equal(mask('2348012345678@s.whatsapp.net'), '234***78')
  assert.equal(mask('123@lid'), '***')
  assert.equal(mask(null), '')
})

test('bounded containers evict the oldest', () => {
  const s = new BoundedSet(2).add('a').add('b').add('c')
  assert.ok(!s.has('a') && s.has('b') && s.has('c'))
  const m = new BoundedMap(2).set('a', 1).set('b', 2).set('c', 3)
  assert.equal(m.get('a'), undefined); assert.equal(m.size, 2)
})
