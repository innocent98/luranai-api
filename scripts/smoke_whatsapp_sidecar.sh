#!/usr/bin/env bash
# Post-deploy smoke for the WhatsApp sidecar. Prints PASS/FAIL lines only; exits 1 if any check fails.
#
#   scripts/smoke_whatsapp_sidecar.sh <sidecar container name or id>
#   SMOKE_BACKEND=disabled scripts/smoke_whatsapp_sidecar.sh <container>   # production before WHATSAPP_ENABLED is flipped
#
# SMOKE_BACKEND says what the BACKEND's WHATSAPP_ENABLED is (default `enabled`, which is what staging runs and is unchanged):
#   enabled  - a signed webhook must be acknowledged `ignored` and a badly signed one refused 401.
#   disabled - the backend answers every webhook 503 `whatsapp_disabled` before it looks at the signature, so the network
#              path is proven (checked as 503 whatsapp_disabled) but the signature path CANNOT be: that check is reported as
#              SKIP, not PASS. Re-run with the default after enabling the backend.
#
# The checks run INSIDE the sidecar container (via `docker exec`), so the bearer token, the webhook secret and the database
# and Redis URLs are read from that container's own environment and never cross the shell, a CD log or this script.
# Output is limited to check names, pass/fail, HTTP status codes, backend error codes and non-secret counters.
#
# What it proves (no tenant, no WhatsApp account and no real session is touched):
#   1. GET /health with the bearer token answers 200 (process up, schema bootstrapped).
#   2. The same request without the token is refused (auth is on).
#   3. A correctly signed `session.status` webhook for a non-existent session reaches the backend over the compose network
#      and is acknowledged `ignored` (network path sidecar -> http://api:8000, shared secret, WHATSAPP_ENABLED on, schema).
#   4. A badly signed webhook is refused 401.
#   5. Redis (logical DB for leases) answers and takes a short-lived write.
#   6. Postgres: connected as the dedicated role, which can read its own schema and has no SELECT on any table in `public`.
#   7. Postgres: the role cannot CREATE in the database or in schema `public` (the schema-hijack guard).
set -euo pipefail

container="${1:?usage: smoke_whatsapp_sidecar.sh <sidecar container name or id>}"
backend="${SMOKE_BACKEND:-enabled}"
case "$backend" in
  enabled|disabled) ;;
  *) echo "SMOKE_BACKEND must be 'enabled' or 'disabled'" >&2; exit 2 ;;
esac

docker exec -i -e "SMOKE_BACKEND=${backend}" "$container" node --input-type=module - <<'JS'
import { createHmac, randomUUID } from 'node:crypto'
import Redis from 'ioredis'
import pg from 'pg'

const env = process.env
let failed = false
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` - ${detail}` : ''}`)
  if (!ok) failed = true
}
const errName = (e) => `${e?.name || 'Error'}${e?.code ? `:${e.code}` : ''}`
const sign = (secret, body) => {
  const t = Math.floor(Date.now() / 1000)
  return `t=${t},v1=${createHmac('sha256', secret).update(`${t}.${body}`).digest('hex')}`
}
const json = async (res) => { try { return await res.json() } catch { return {} } }

const local = `http://127.0.0.1:${env.PORT || 8787}`
try {
  const res = await fetch(`${local}/health`, { headers: { authorization: `Bearer ${env.WHATSAPP_SIDECAR_TOKEN}` }, signal: AbortSignal.timeout(5000) })
  const body = await json(res)
  check('sidecar /health with token', res.status === 200 && typeof body.sessions === 'number',
    `status=${res.status} sessions=${body.sessions}/${body.max_sessions} rss_mb=${body.rss_mb}/${body.rss_refuse_mb}`)
} catch (e) { check('sidecar /health with token', false, errName(e)) }

try {
  const res = await fetch(`${local}/health`, { signal: AbortSignal.timeout(5000) })
  check('sidecar /health without token is refused', res.status === 401, `status=${res.status}`)
} catch (e) { check('sidecar /health without token is refused', false, errName(e)) }

const hook = `${String(env.BACKEND_BASE || '').replace(/\/+$/, '')}/api/v1/webhooks/whatsapp/linked`
const event = JSON.stringify({
  id: randomUUID(), at: new Date().toISOString(), session_key: 'smoke-probe-no-such-session', type: 'session.status', status: 'connecting',
})
const post = (signature) => fetch(hook, {
  method: 'POST', body: event, redirect: 'error', signal: AbortSignal.timeout(10000),
  headers: { 'content-type': 'application/json', 'x-luran-signature': signature },
})
const backendDisabled = env.SMOKE_BACKEND === 'disabled'
try {
  const res = await post(sign(env.WHATSAPP_WEBHOOK_SECRET, event))
  const body = await json(res)
  const detail = `status=${res.status}${body.error ? ` error=${body.error}` : ''}${body.status ? ` result=${body.status}` : ''}`
  if (backendDisabled) {
    check('backend reachable over the compose network and reports WhatsApp disabled', res.status === 503 && body.error === 'whatsapp_disabled', detail)
  } else {
    check('signed webhook reaches the backend and is acknowledged', res.status === 200 && body.status === 'ignored', detail)
  }
} catch (e) { check(backendDisabled ? 'backend reachable over the compose network and reports WhatsApp disabled' : 'signed webhook reaches the backend and is acknowledged', false, errName(e)) }
if (backendDisabled) {
  // The disabled backend answers 503 before it verifies the signature, so a bad signature cannot be told from a good one.
  console.log('SKIP badly signed webhook is refused - not testable while the backend has WHATSAPP_ENABLED off; re-run without SMOKE_BACKEND after enabling')
} else {
  try {
    const res = await post(sign(randomUUID() + randomUUID(), event))
    const body = await json(res)
    check('badly signed webhook is refused', res.status === 401 && body.error === 'invalid_signature',
      `status=${res.status}${body.error ? ` error=${body.error}` : ''}`)
  } catch (e) { check('badly signed webhook is refused', false, errName(e)) }
}

const redis = new Redis(env.WHATSAPP_SIDECAR_REDIS_URL, { enableOfflineQueue: false, maxRetriesPerRequest: 1, commandTimeout: 3000, lazyConnect: true })
redis.on('error', () => {})
try {
  await redis.connect()
  await redis.set('wa:smoke:probe', '1', 'EX', 30)
  const got = await redis.get('wa:smoke:probe')
  await redis.del('wa:smoke:probe')
  check('redis ping/set/get/del on the sidecar logical DB', got === '1')
} catch (e) { check('redis ping/set/get/del on the sidecar logical DB', false, errName(e)) } finally { redis.disconnect() }

const client = new pg.Client({ connectionString: env.WHATSAPP_SIDECAR_DATABASE_URL, connectionTimeoutMillis: 8000 })
client.on('error', () => {})
try {
  await client.connect()
  const who = (await client.query('SELECT current_user AS u, current_schema() AS s')).rows[0]
  const own = (await client.query('SELECT count(*)::int AS n FROM whatsapp_sidecar.sessions')).rows[0]
  const leak = (await client.query(
    `SELECT count(*)::int AS n FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','p') AND has_table_privilege(current_user, c.oid, 'SELECT')`)).rows[0]
  const dedicated = who.u === 'whatsapp_sidecar'
  check('postgres: dedicated role reads its own schema', dedicated && Number.isInteger(own.n), `role=${who.u} sessions_rows=${own.n}`)
  check('postgres: no SELECT on any public table', leak.n === 0, `readable_public_tables=${leak.n}`)
  const create = (await client.query(
    `SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS db,
            has_schema_privilege(current_user, 'public', 'CREATE') AS public_schema`)).rows[0]
  check('postgres: no CREATE on the database or on schema public', create.db === false && create.public_schema === false,
    `database_create=${create.db} public_schema_create=${create.public_schema}`)
} catch (e) { check('postgres: dedicated role reads its own schema', false, errName(e)) } finally { await client.end().catch(() => {}) }

process.exitCode = failed ? 1 : 0
JS
