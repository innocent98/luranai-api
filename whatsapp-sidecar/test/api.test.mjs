import test from 'node:test'
import assert from 'node:assert/strict'
import { bearerOk, createApi } from '../src/api.mjs'
import { TOKEN, makeHarness, pairAndConnect, textMsg, until } from './helpers.mjs'

const CUST = '2348012345678@s.whatsapp.net'

async function boot(over) {
  const h = makeHarness(over)
  const server = createApi({ manager: h.mgr, token: TOKEN, log: h.logCap.log })
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const base = `http://127.0.0.1:${server.address().port}`
  const call = async (method, path, body, token = TOKEN) => {
    const r = await fetch(base + path, { method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), 'content-type': 'application/json' }, body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body) })
    return { status: r.status, body: await r.json() }
  }
  return { h, call, close: () => new Promise((r) => { server.close(r); server.closeAllConnections() }) }
}

test('bearer compare: exact token only', () => {
  assert.ok(bearerOk(`Bearer ${TOKEN}`, TOKEN))
  for (const bad of [undefined, '', TOKEN, `bearer ${TOKEN}`, `Bearer ${TOKEN}x`, `Bearer ${TOKEN.slice(1)}`, 'Bearer ']) assert.ok(!bearerOk(bad, TOKEN))
})

test('every route requires the bearer token', async () => {
  const { call, close } = await boot()
  for (const [m, p] of [['GET', '/health'], ['POST', '/sessions'], ['GET', '/sessions/a'], ['DELETE', '/sessions/a'], ['POST', '/sessions/a/send'], ['POST', '/sessions/a/pairing-code']]) {
    assert.deepEqual(await call(m, p, undefined, null), { status: 401, body: { error: 'unauthorized' } })
    assert.equal((await call(m, p, undefined, 'wrong')).status, 401)
  }
  await close()
})

test('contract: create -> 201, duplicate 409, get, pairing-code, send errors, delete, health', async () => {
  const { h, call, close } = await boot()
  assert.deepEqual((await call('GET', '/health')).body, { sessions: 0, max_sessions: 3, rss_mb: 150, rss_refuse_mb: 400 })

  const c = await call('POST', '/sessions', { session_key: 'tenant-a', phone_digits: '2348000000001' })
  assert.deepEqual(c, { status: 201, body: { status: 'awaiting_pair', pairing_code: 'ABCD1234', expires_in_s: 180 } })
  assert.deepEqual(await call('POST', '/sessions', { session_key: 'tenant-a', phone_digits: '2348000000001' }), { status: 409, body: { error: 'session_exists' } })

  const g = await call('GET', '/sessions/tenant-a')
  assert.equal(g.status, 200); assert.equal(g.body.status, 'awaiting_pair'); assert.equal(typeof g.body.since_ms, 'number'); assert.equal(g.body.last_disconnect, null)
  assert.deepEqual(Object.keys(g.body).sort(), ['last_disconnect', 'since_ms', 'status'])
  assert.deepEqual(await call('GET', '/sessions/nope'), { status: 404, body: { error: 'not_found' } })

  assert.deepEqual(await call('POST', '/sessions/tenant-a/pairing-code'), { status: 200, body: { pairing_code: 'ABCD1234', expires_in_s: 180 } })

  assert.deepEqual(await call('POST', '/sessions/tenant-a/send', { to_ref: CUST, text: 'x' }), { status: 503, body: { error: 'not_connected' } })
  const sock = h.last(), n = h.sockets.length
  sock.opts.auth.state.creds.me = { id: '2348000000001:1@s.whatsapp.net' }; sock.opts.auth.state.creds.registered = true; sock.close(515)
  await until(() => h.sockets.length === n + 1); h.last().open(); await until(() => h.mgr.get('tenant-a').status === 'connected')
  assert.deepEqual(await call('POST', '/sessions/tenant-a/pairing-code'), { status: 409, body: { error: 'not_pairable', status: 'connected' } })
  assert.deepEqual(await call('POST', '/sessions/tenant-a/send', { to_ref: CUST, text: 'x' }), { status: 409, body: { error: 'reply_only' } })
  h.last().upsert([textMsg('IN1', CUST, 'hi')]); await until(() => h.events('message.inbound').length === 1)
  const s = await call('POST', '/sessions/tenant-a/send', { to_ref: CUST, text: 'hello' })
  assert.equal(s.status, 200); assert.deepEqual(Object.keys(s.body), ['message_id'])

  assert.equal((await call('POST', '/sessions/tenant-a/send', { to_ref: CUST })).status, 400)
  assert.equal((await call('POST', '/sessions/tenant-a/send', { to_ref: CUST, text: 'x'.repeat(5000) })).status, 400)
  assert.equal((await call('POST', '/sessions', '{not json')).status, 400)
  assert.equal((await call('POST', '/sessions', [])).status, 400)

  assert.deepEqual(await call('DELETE', '/sessions/tenant-a'), { status: 200, body: { status: 'logged_out' } })
  assert.deepEqual(await call('DELETE', '/sessions/tenant-a'), { status: 200, body: { status: 'logged_out' } })
  assert.equal((await call('GET', '/sessions/tenant-a')).body.status, 'logged_out')
  await close()
})

test('capacity over HTTP: 503 {error, max_sessions}; rate cap 429 {error:rate_cap}', async () => {
  const { h, call, close } = await boot({ cfg: { maxSessions: 1, sendPerMin: 1 } })
  await pairAndConnect(h, 'a', '2348000000001')
  assert.deepEqual(await call('POST', '/sessions', { session_key: 'b', phone_digits: '2348000000002' }), { status: 503, body: { error: 'capacity_exceeded', max_sessions: 1 } })
  h.last().upsert([textMsg('IN1', CUST, 'hi')]); await until(() => h.events('message.inbound').length === 1)
  assert.equal((await call('POST', '/sessions/a/send', { to_ref: CUST, text: '1' })).status, 200)
  assert.deepEqual(await call('POST', '/sessions/a/send', { to_ref: CUST, text: '2' }), { status: 429, body: { error: 'rate_cap' } })
  await close()
})

test('unknown routes and methods; oversized body', async () => {
  const { call, close } = await boot()
  assert.equal((await call('GET', '/nope')).status, 404)
  assert.equal((await call('PUT', '/sessions/a')).status, 405)
  assert.equal((await call('GET', '/sessions/../etc')).status, 404)
  const big = await call('POST', '/sessions', JSON.stringify({ session_key: 'a', phone_digits: '1'.repeat(100000) })).catch((e) => ({ status: 'reset' }))
  assert.ok(big.status === 413 || big.status === 'reset')
  await close()
})

test('responses never contain secrets; logs never contain pairing code or phone', async () => {
  const { h, call, close } = await boot()
  await call('POST', '/sessions', { session_key: 'tenant-a', phone_digits: '2348000000001' })
  const out = h.logCap.text()
  assert.ok(!out.includes('ABCD1234')); assert.ok(!out.includes('2348000000001')); assert.ok(!out.includes(TOKEN))
  await close()
})
