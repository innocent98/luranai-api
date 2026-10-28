import test from 'node:test'
import assert from 'node:assert/strict'
import { MemoryLeaseBackend } from '../src/lease.mjs'
import { classifyDisconnect } from '../src/status.mjs'
import { makeHarness, pairAndConnect, textMsg, tick, until } from './helpers.mjs'

const CUST = '2348012345678@s.whatsapp.net'
const LID = '77123456@lid'

test('create: code requested once on the first qr, contract response, awaiting_pair event, code never logged or emitted', async () => {
  const h = makeHarness()
  const res = await h.mgr.create({ session_key: 'tenant-a', phone_digits: '2348000000001' })
  assert.deepEqual(res, { status: 'awaiting_pair', pairing_code: 'ABCD1234', expires_in_s: 180 })
  const s = h.last()
  assert.deepEqual(s.calls.filter((c) => c[0] === 'pair'), [['pair', '2348000000001']])
  s.update({ qr: 'again' }); s.update({ qr: 'again' }); await tick(10)
  assert.equal(s.calls.filter((c) => c[0] === 'pair').length, 1)             // qr refreshes never re-request
  assert.deepEqual(h.events('session.status').map((e) => e.status), ['awaiting_pair'])
  assert.equal(s.opts.auth.state.creds.registered, false)
  assert.ok(!JSON.stringify(h.emitted).includes('ABCD1234'))
  assert.ok(!h.logCap.text().includes('ABCD1234'))
  assert.ok(!h.logCap.text().includes('2348000000001'))
})

test('pairing flow: 515 after pairing reconnects (connecting -> connected), no new code requested', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  assert.equal(h.sockets.length, 2)
  assert.equal(h.sockets[1].calls.filter((c) => c[0] === 'pair').length, 0)
  assert.deepEqual(h.events('session.status').map((e) => e.status), ['awaiting_pair', 'connecting', 'connected'])
  assert.equal((await h.db.getSession('tenant-a')).registered, true)
  assert.deepEqual(h.mgr.get('tenant-a').snapshot().last_disconnect, null)
})

test('no code and clean failure when the socket never becomes ready', async () => {
  const h = makeHarness({ socketOpts: { autoQr: false }, cfg: { pairingWaitMs: 40 } })
  await assert.rejects(h.mgr.create({ session_key: 'tenant-a', phone_digits: '2348000000001' }), (e) => e.status === 504 && e.code === 'pairing_code_timeout')
  assert.equal(h.mgr.sessions.size, 0)
  assert.equal(await h.db.getSession('tenant-a'), null)            // retry-able: nothing left behind
  assert.ok(h.sockets[0].ended)
})

test('regenerating a code uses a fresh socket and only on an explicit call', async () => {
  const h = makeHarness()
  await h.mgr.create({ session_key: 'tenant-a', phone_digits: '2348000000001' })
  const first = h.last()
  h.sockets[0].pairCode = 'ABCD1234'
  const again = await h.mgr.requestPairingCode('tenant-a')
  assert.equal(again.pairing_code, 'ABCD1234'); assert.equal(again.expires_in_s, 180)
  assert.equal(h.sockets.length, 2); assert.ok(first.ended)
  first.close(408)                                                  // stale socket events are ignored
  await tick(10)
  assert.equal(h.mgr.get('tenant-a').status, 'awaiting_pair')
})

test('pairing-code is refused unless awaiting_pair or logged_out', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  await assert.rejects(h.mgr.requestPairingCode('tenant-a'), (e) => e.status === 409 && e.code === 'not_pairable')
  await assert.rejects(h.mgr.requestPairingCode('nope'), (e) => e.status === 404)
})

test('unpaired socket that dies is not auto-retried; the sweeper expires it to logged_out(408)', async () => {
  const h = makeHarness()
  await h.mgr.create({ session_key: 'tenant-a', phone_digits: '2348000000001' })
  h.last().close(408)
  await tick(30)
  assert.equal(h.sockets.length, 1)                                   // no reconnect, no second code
  assert.equal(h.mgr.get('tenant-a').status, 'awaiting_pair')
  h.clock.now += h.cfg.pairingTtlMs + 1
  const s = h.mgr.get('tenant-a'); assert.ok(s.pairingExpired())
  await s.expirePairing()
  assert.equal(s.status, 'logged_out')
  assert.equal(h.events('session.status').at(-1).code, 408)
  assert.equal(h.mgr.activeCount(), 0)
  await h.mgr.requestPairingCode('tenant-a')                          // can pair again
  assert.equal(h.mgr.get('tenant-a').status, 'awaiting_pair')
})

// ---- status mapping ----
test('status mapping table (pure)', () => {
  const c = (o) => classifyDisconnect({ registered: true, leaseHeld: true, ...o })
  assert.equal(c({ code: 515 }).action, 'restart')
  assert.equal(c({ code: 515, consecutiveRestarts: 5 }).action, 'reconnect')
  assert.deepEqual(c({ code: 401 }), { action: 'logout', status: 'logged_out' })
  assert.equal(c({ code: 401, leaseHeld: false }).action, 'lease_lost')
  assert.equal(c({ code: 403 }).status, 'failed')
  for (const code of [408, 428, 500, 503, 411, undefined]) assert.equal(c({ code }).status, 'reconnecting')
  assert.equal(c({ code: 440 }).status, 'reconnecting')
  assert.equal(c({ code: 440, consecutiveConflicts: 3 }).status, 'failed')
  assert.equal(c({ code: 428, registered: false }).action, 'idle')
})

test('401 while holding the lease: logged_out, credentials wiped, lease released', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  assert.ok(h.db.blobCount('tenant-a') > 0)
  h.last().close(401, 'Stream Errored (conflict)')
  await until(() => h.mgr.get('tenant-a').status === 'logged_out')
  assert.equal(h.db.blobCount('tenant-a'), 0)
  assert.equal(h.events('session.status').at(-1).code, 401)
  assert.equal(h.leaseBackend.map.size, 0)
  assert.deepEqual(h.mgr.get('tenant-a').snapshot().last_disconnect.code, 401)
})

test('401 when the lease cannot be proven: credentials are NOT wiped; session retries instead', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  const before = h.db.blobCount('tenant-a')
  h.leaseBackend.steal('wa:lease:tenant-a', 'other-process', 30000)   // someone else holds it
  h.last().close(401)
  await until(() => h.mgr.get('tenant-a').status === 'reconnecting')
  assert.equal(h.db.blobCount('tenant-a'), before)
  await tick(30)
  assert.equal(h.mgr.get('tenant-a').status, 'reconnecting')          // cannot reacquire: stays stopped, still no wipe
  assert.equal(h.sockets.length, 2)                                    // no new socket while another process owns the lease
})

test('lease lost while connected: socket stopped, nothing wiped or flushed; resumes once the lease is free', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  const s = h.mgr.get('tenant-a'), sock = h.last(), before = h.db.blobCount('tenant-a')
  await s.auth.state.keys.set({ session: { late: Buffer.from('x') } })   // pending write that must not be flushed after losing ownership
  s.lease._lose('test')
  assert.ok(sock.ended)
  assert.equal(s.status, 'reconnecting'); assert.equal(h.db.blobCount('tenant-a'), before)
  await until(() => h.sockets.length === 3)                             // lease is free (we released) -> reacquired -> new socket
  h.last().open()
  await until(() => s.status === 'connected')
  assert.equal(h.db.blobCount('tenant-a'), before)
})

test('515 restart-required reconnects immediately without wiping', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  const n = h.db.blobCount('tenant-a')
  h.last().close(515)
  await until(() => h.sockets.length === 3)
  assert.equal(h.mgr.get('tenant-a').status, 'connecting'); assert.equal(h.db.blobCount('tenant-a'), n)
  h.last().open(); await until(() => h.mgr.get('tenant-a').status === 'connected')
})

test('other codes: reconnecting with backoff, then connected again; no wipe', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  h.last().close(428)
  await until(() => h.mgr.get('tenant-a').status === 'reconnecting')
  assert.equal(h.events('session.status').at(-1).code, 428)
  await until(() => h.sockets.length === 3)
  h.last().open(); await until(() => h.mgr.get('tenant-a').status === 'connected')
})

test('403 (restricted) fails the session and never reconnects', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  h.last().close(403)
  await until(() => h.mgr.get('tenant-a').status === 'failed')
  await tick(30); assert.equal(h.sockets.length, 2)
  assert.ok(h.db.blobCount('tenant-a') > 0)
})

test('persistent conflict (440 x3) fails the session without wiping', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  for (let i = 0; i < 3; i++) { const n = h.sockets.length; h.last().close(440); await until(() => h.sockets.length === n + 1 || h.mgr.get('tenant-a').status === 'failed') }
  await until(() => h.mgr.get('tenant-a').status === 'failed')
  assert.ok(h.db.blobCount('tenant-a') > 0)
})

test('Redis down at reconnect: fail closed (no socket), recovers when Redis is back', async () => {
  const h = makeHarness()
  await pairAndConnect(h)
  h.leaseBackend.down = true
  h.last().close(428)
  await tick(40)
  assert.equal(h.sockets.length, 2); assert.equal(h.mgr.get('tenant-a').status, 'reconnecting')
  h.leaseBackend.down = false
  await until(() => h.sockets.length === 3)
})

// ---- inbound ----
test('inbound text: contract fields; from_ref kept opaque; phone resolved from jid, remoteJidAlt or the lid mapping', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.last(); s.lidMap[LID] = '2348099999999@s.whatsapp.net'
  s.upsert([textMsg('A1', CUST, 'hi there')])
  s.upsert([textMsg('A2', LID, 'via lid')])
  s.upsert([{ ...textMsg('A3', '55@lid', 'alt'), key: { id: 'A3', remoteJid: '55@lid', remoteJidAlt: '2347011111111@s.whatsapp.net', fromMe: false } }])
  s.upsert([textMsg('A4', '88@lid', 'unknown')])
  await until(() => h.events('message.inbound').length === 4)
  const ev = h.events('message.inbound')
  assert.deepEqual(ev[0], { session_key: 'tenant-a', type: 'message.inbound', message_id: 'A1', from_ref: CUST, from_phone_digits: '2348012345678', push_name: 'Ada', kind: 'text', text: 'hi there' })
  assert.equal(ev[1].from_ref, LID); assert.equal(ev[1].from_phone_digits, '2348099999999')
  assert.equal(ev[2].from_phone_digits, '2347011111111')
  assert.equal(ev[3].from_phone_digits, null)
})

test('inbound: non-image media is kind other with empty text (never downloaded; images: see media-session.test.mjs); extended text works; duplicates, groups, status, history ignored', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.last()
  s.upsert([
    { key: { id: 'M1', remoteJid: CUST, fromMe: false }, message: { videoMessage: { caption: 'look', mimetype: 'video/mp4' } } },
    { key: { id: 'M2', remoteJid: CUST, fromMe: false }, message: { extendedTextMessage: { text: 'quoted reply' } } },
    textMsg('M2', CUST, 'dup'),
    textMsg('G1', '1203630@g.us', 'group'),
    textMsg('S1', 'status@broadcast', 'status'),
    { key: { id: 'R1', remoteJid: CUST, fromMe: false }, message: { reactionMessage: { text: '👍' } } },
    { key: { id: 'N1', remoteJid: CUST, fromMe: false }, message: null },
    { key: { id: '../../etc', remoteJid: CUST, fromMe: false }, message: { conversation: 'bad id' } },
  ])
  s.upsert([textMsg('H1', CUST, 'history')], 'append')
  await tick(30)
  const ev = h.events('message.inbound')
  assert.deepEqual(ev.map((e) => [e.message_id, e.kind, e.text]), [['M1', 'other', ''], ['M2', 'text', 'quoted reply']])
})

// ---- owner detection ----
test('owner typed on the phone: message.outbound_external; our own /send echoes are not', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.last()
  s.upsert([textMsg('IN1', CUST, 'hello')]); await until(() => h.events('message.inbound').length === 1)
  s.upsert([{ key: { id: 'PH1', remoteJid: CUST, fromMe: true }, message: { conversation: 'I will take over' }, messageTimestamp: 1_800_000_000 }])
  await until(() => h.events('message.outbound_external').length === 1)
  assert.deepEqual(h.events('message.outbound_external')[0], { session_key: 'tenant-a', type: 'message.outbound_external', message_id: 'PH1', to_ref: CUST, text: 'I will take over' })
  const sent = await h.mgr.send('tenant-a', 'x', 'x').catch(() => null)   // invalid ref, no effect
  assert.equal(sent, null)
  const r = await h.mgr.send('tenant-a', CUST, 'AI reply')
  s.upsert([{ key: { id: r.message_id, remoteJid: CUST, fromMe: true }, message: { conversation: 'AI reply' }, messageTimestamp: 1_800_000_000 }], 'append')
  s.upsert([{ key: { id: r.message_id, remoteJid: CUST, fromMe: true }, message: { conversation: 'AI reply' }, messageTimestamp: 1_800_000_000 }], 'notify')
  await tick(20)
  assert.equal(h.events('message.outbound_external').length, 1)
})

test('owner activity: duplicates, edits, deletes, reactions, groups, self chat and stale history are not emitted; owner media is', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.last(), ts = 1_800_000_000
  const out = (id, message, extra = {}) => ({ key: { id, remoteJid: CUST, fromMe: true, ...extra }, message, messageTimestamp: ts })
  s.upsert([
    out('O1', { conversation: 'one' }), out('O1', { conversation: 'one' }),
    out('E1', { protocolMessage: { type: 14, editedMessage: { conversation: 'edit' } } }),
    out('D1', { protocolMessage: { type: 0 } }),
    out('E2', { editedMessage: { message: { conversation: 'edit2' } } }),
    out('R1', { reactionMessage: { text: '❤' } }),
    { key: { id: 'GG', remoteJid: '12@g.us', fromMe: true }, message: { conversation: 'grp' }, messageTimestamp: ts },
    { key: { id: 'SELF', remoteJid: '15550001111@s.whatsapp.net', fromMe: true }, message: { conversation: 'note to self' }, messageTimestamp: ts },
    { ...out('OLD', { conversation: 'ancient' }), messageTimestamp: ts - 7200 },
    out('IMG', { imageMessage: { caption: 'photo' } }),
  ])
  await tick(30)
  assert.deepEqual(h.events('message.outbound_external').map((e) => [e.message_id, e.text]), [['O1', 'one'], ['IMG', '']])
})

// ---- sending ----
test('reply-only: 409 until the chat has messaged us; then allowed; window enforced; survives a restart', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  await assert.rejects(h.mgr.send('tenant-a', CUST, 'cold outreach'), (e) => e.status === 409 && e.code === 'reply_only')
  assert.equal(h.last().calls.filter((c) => c[0] === 'send').length, 0)
  h.last().upsert([textMsg('IN1', CUST, 'hello')]); await until(() => h.events('message.inbound').length === 1)
  const r = await h.mgr.send('tenant-a', CUST, 'hi!')
  assert.match(r.message_id, /^OUT/)
  await assert.rejects(h.mgr.send('tenant-a', '2347000000000@s.whatsapp.net', 'other number'), (e) => e.code === 'reply_only')
  h.clock.now += 25 * 3600 * 1000
  await assert.rejects(h.mgr.send('tenant-a', CUST, 'a day later'), (e) => e.code === 'reply_only')   // outside the 24 h window
  // fresh process, same database: the persisted chat is still known
  h.clock.now -= 25 * 3600 * 1000
  const h2 = makeHarness({ db: h.db }); h2.deps.leaseBackend = h2.leaseBackend
  await h.mgr.shutdown()
  await h2.mgr.restoreAll(); await until(() => h2.sockets.length === 1)
  h2.last().open(); await until(() => h2.mgr.get('tenant-a').status === 'connected')
  assert.ok((await h2.mgr.send('tenant-a', CUST, 'after restart')).message_id)
})

test('the database never stores the raw chat id or phone number', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.last().upsert([textMsg('IN1', CUST, 'hello')]); await until(() => h.events('message.inbound').length === 1)
  await tick(10)
  const keys = [...h.db.chats.keys()].join('|')
  assert.ok(keys.length > 0)
  assert.ok(!keys.includes('2348012345678'))
  assert.ok(!(await h.db.getSession('tenant-a')).phone_enc.includes('2348000000001'))
})

test('send order: read receipt, composing, delay, paused, then the message (id registered first)', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.last(); s.upsert([textMsg('IN1', CUST, 'hello')]); await until(() => h.events('message.inbound').length === 1)
  h.sleeps.length = 0
  await h.mgr.send('tenant-a', CUST, 'x'.repeat(100))
  assert.deepEqual(s.names().filter((n) => n !== 'pair'), ['read', 'sub', 'presence', 'presence', 'send'])
  assert.deepEqual(s.calls.find((c) => c[0] === 'read'), ['read', 'IN1'])
  assert.deepEqual(s.calls.filter((c) => c[0] === 'presence').map((c) => c[1]), ['composing', 'paused'])
  const delay = h.sleeps.at(-1)
  assert.ok(delay >= 1500 + 100 * 25 && delay <= 1500 + 2500 + 3000, `delay ${delay}`)
  assert.ok(s.calls.find((c) => c[0] === 'send')[3])                   // explicit message id passed to Baileys
})

test('rate cap: 429 rate_cap after the per-minute bucket, refills over time; daily cap holds', async () => {
  const h = makeHarness({ cfg: { sendPerMin: 2, dailySendCap: 3 } }); await pairAndConnect(h)
  h.last().upsert([textMsg('IN1', CUST, 'hello')]); await until(() => h.events('message.inbound').length === 1)
  await h.mgr.send('tenant-a', CUST, 'a'); await h.mgr.send('tenant-a', CUST, 'b')
  await assert.rejects(h.mgr.send('tenant-a', CUST, 'c'), (e) => e.status === 429 && e.code === 'rate_cap')
  h.clock.now += 31_000
  await h.mgr.send('tenant-a', CUST, 'c')                              // third today
  h.clock.now += 120_000
  await assert.rejects(h.mgr.send('tenant-a', CUST, 'd'), (e) => e.status === 429 && e.scope === undefined ? true : e.extra.scope === 'daily')
  assert.equal(h.last().calls.filter((c) => c[0] === 'send').length, 3)
})

test('not connected: 503 not_connected (reconnecting, awaiting_pair, logged_out)', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.last().close(428); await until(() => h.mgr.get('tenant-a').status === 'reconnecting')
  await assert.rejects(h.mgr.send('tenant-a', CUST, 'x'), (e) => e.status === 503 && e.code === 'not_connected')
  await assert.rejects(h.mgr.send('missing', CUST, 'x'), (e) => e.status === 404)
})

test('sends are serialised per session (no parallel burst)', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.last(); s.upsert([textMsg('IN1', CUST, 'hello')]); await until(() => h.events('message.inbound').length === 1)
  let inFlight = 0, max = 0
  const real = s.sendMessage.bind(s)
  s.sendMessage = async (...a) => { inFlight++; max = Math.max(max, inFlight); await tick(15); const r = await real(...a); inFlight--; return r }
  await Promise.all([h.mgr.send('tenant-a', CUST, '1'), h.mgr.send('tenant-a', CUST, '2'), h.mgr.send('tenant-a', CUST, '3')])
  assert.equal(max, 1)
})

// ---- logout / shutdown ----
test('DELETE: remote logout, creds wiped, lease released, idempotent, can pair again', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const sock = h.last()
  assert.deepEqual(await h.mgr.remove('tenant-a'), { status: 'logged_out' })
  assert.ok(sock.loggedOut)
  assert.equal(h.db.blobCount('tenant-a'), 0); assert.equal(h.leaseBackend.map.size, 0)
  assert.equal(h.mgr.get('tenant-a').status, 'logged_out')
  assert.deepEqual(await h.mgr.remove('tenant-a'), { status: 'logged_out' })
  assert.deepEqual(await h.mgr.remove('never-existed'), { status: 'logged_out' })
  sock.close(401); await tick(20)                                       // our own logout's echo is ignored
  assert.equal(h.events('session.status').filter((e) => e.status === 'logged_out').length, 1)
  const again = await h.mgr.requestPairingCode('tenant-a')
  assert.equal(again.pairing_code, 'ABCD1234')
})

test('SIGTERM path: credentials flushed, sockets closed, leases released, no reconnect afterwards', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.mgr.get('tenant-a'), sock = h.last()
  await s.auth.state.keys.set({ session: { fresh: Buffer.from('v') } })
  const before = h.db.blobCount('tenant-a')
  await h.mgr.shutdown()
  assert.ok(sock.ended); assert.equal(h.leaseBackend.map.size, 0)
  assert.equal(h.db.blobCount('tenant-a'), before + 1)
  await assert.rejects(h.mgr.send('tenant-a', CUST, 'x'), (e) => e.code === 'shutting_down')
  await assert.rejects(h.mgr.create({ session_key: 'tenant-z', phone_digits: '2348000000009' }), (e) => e.code === 'shutting_down')
  sock.close(428); await tick(30); assert.equal(h.sockets.length, 2)
})

test('two processes, one credential set: the second cannot start a socket while the first holds the lease', async () => {
  const shared = new MemoryLeaseBackend()
  const h1 = makeHarness({ leaseBackend: shared }); await pairAndConnect(h1)
  const h2 = makeHarness({ db: h1.db, leaseBackend: shared })
  await h2.mgr.restoreAll(); await tick(40)
  assert.equal(h2.sockets.length, 0)
  assert.equal(h2.mgr.get('tenant-a').status, 'reconnecting')
  assert.equal(h2.emitted.at(-1).code, 440)
  assert.ok(h1.db.blobCount('tenant-a') > 0)
  await h2.mgr.shutdown()
})

// ---- regressions from the security review ----
test('logout while a reconnect is in flight: no socket is started afterwards, credentials stay wiped, lease released', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  let entered = null; const gate = new Promise((r) => { entered = r })
  let release; const hold = new Promise((r) => { release = r })
  h.deps.versionProvider = async () => { entered(); await hold }
  const n = h.sockets.length
  h.last().close(428)
  await gate                                                   // _connect is now suspended mid-flight
  const del = h.mgr.remove('tenant-a')
  release(); await del
  await tick(30)
  assert.equal(h.sockets.length, n)
  assert.equal(h.mgr.get('tenant-a').status, 'logged_out')
  assert.equal(h.db.blobCount('tenant-a'), 0); assert.equal(h.leaseBackend.map.size, 0)
})

test('DELETE while another process owns the lease: refused, credentials NOT wiped', async () => {
  const shared = new MemoryLeaseBackend()
  const h1 = makeHarness({ leaseBackend: shared }); await pairAndConnect(h1)
  const h2 = makeHarness({ db: h1.db, leaseBackend: shared })
  await h2.mgr.restoreAll(); await tick(30)
  const n = h1.db.blobCount('tenant-a')
  await assert.rejects(h2.mgr.remove('tenant-a'), (e) => e.status === 409 && e.code === 'session_locked')
  assert.equal(h1.db.blobCount('tenant-a'), n)
  assert.equal(h1.mgr.get('tenant-a').status, 'connected')
  await h2.mgr.shutdown()
})

test('a stale Lease object cannot tear down the current session', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const s = h.mgr.get('tenant-a')
  s._onLeaseLost({ some: 'old lease' })
  assert.equal(s.status, 'connected'); assert.ok(!h.last().ended)
})

test('401 after the lease key was evicted and re-created is NOT proof of ownership: no wipe, converges afterwards', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  const n = h.db.blobCount('tenant-a')
  h.leaseBackend.evict('wa:lease:tenant-a')
  h.last().close(401)
  await until(() => h.mgr.get('tenant-a').status === 'reconnecting')
  assert.equal(h.db.blobCount('tenant-a'), n)
  await until(() => h.sockets.length === 3)                    // reconnects; lease is now provably ours again
  h.last().close(401)                                          // second 401 under a proven lease is a real logout
  await until(() => h.mgr.get('tenant-a').status === 'logged_out')
  assert.equal(h.db.blobCount('tenant-a'), 0)
})

test('pairing-code on several logged_out sessions cannot exceed MAX_SESSIONS', async () => {
  const h = makeHarness({ cfg: { maxSessions: 1 } })
  await h.mgr.create({ session_key: 'a', phone_digits: '2348000000001' }); await h.mgr.remove('a')
  await h.mgr.create({ session_key: 'b', phone_digits: '2348000000002' }); await h.mgr.remove('b')
  const r = await Promise.allSettled([h.mgr.requestPairingCode('a'), h.mgr.requestPairingCode('b')])
  assert.equal(r.filter((x) => x.status === 'fulfilled').length, 1)
  assert.equal(h.mgr.activeCount(), 1)
})

test('create rejects non-string body fields (no unreachable sessions)', async () => {
  const h = makeHarness()
  for (const bad of [{ session_key: 123, phone_digits: '2348000000001' }, { session_key: ['abc'], phone_digits: '2348000000001' }, { session_key: 'ok', phone_digits: 2348000000001 }, { session_key: 'ok', phone_digits: ['2348000000001'] }]) {
    await assert.rejects(h.mgr.create(bad), (e) => e.status === 400)
  }
  assert.equal(h.sockets.length, 0)
})

test('errors from the socket layer are logged by class only (no identifiers)', async () => {
  const h = makeHarness(); await pairAndConnect(h)
  h.deps.makeSocket = () => { throw new Error('boom for 2348012345678@s.whatsapp.net') }
  h.last().close(428)
  await tick(40)
  assert.ok(!h.logCap.text().includes('2348012345678'))
})
