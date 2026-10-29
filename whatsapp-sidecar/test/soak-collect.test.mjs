// Soak collector: probe script, docker/url probes, step file, sampling loop and CLI. Docker is replaced by an injected exec;
// the probe script itself is run for real with local bash against a local HTTP stub (no sidecar, no network, no WhatsApp).
import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import { execFile, spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { promisify } from 'node:util'
import { PROBE_SCRIPT, makeDockerProbe, makeUrlProbe, openCsv, readStep, runLoop, sampleOnce, stepFile } from '../scripts/soak.mjs'
import { csvHeader, parseCsv, parseProbeOutput } from '../scripts/soak-lib.mjs'

const run = promisify(execFile)
const dir = () => mkdtempSync(join(tmpdir(), 'soak-'))
const SOAK = new URL('../scripts/soak.mjs', import.meta.url).pathname
const CAPTURE = 'cg current 10022912\ncg max 402653184\ncg anon 8278016\ncg file 0\ncg shmem 0\ncg slab_reclaimable 124840\nev max 0\nev oom 0\nev oom_kill 0\n' +
  'http /health 200 {"sessions":2,"max_sessions":3,"rss_mb":45.3,"rss_refuse_mb":300}\nhttp /sessions/a 200 {"status":"connected","since_ms":5,"last_disconnect":null}\n'

const fakeExec = (log, { execOut = CAPTURE, inspectOut = '2026-10-28T09:00:00.123456789Z|false|true|0\n', failExec = false } = {}) => async (bin, args) => {
  log.push([bin, args])
  if (args[0] === 'exec') { if (failExec) throw Object.assign(new Error('SECRET stderr text'), { stderr: 'SECRET stderr text' }); return { stdout: execOut } }
  return { stdout: inspectOut }
}

test('docker probe: one exec per sample, keys are positional args, script is a constant, inspect adds start/OOM facts', async () => {
  const log = []
  const probe = makeDockerProbe({ container: 'luran_staging_whatsapp_sidecar', keys: ['a', 'b'], exec: fakeExec(log) })
  const res = await probe()
  assert.equal(res.error, null)
  assert.deepEqual(log[0][1].slice(0, 4), ['exec', 'luran_staging_whatsapp_sidecar', 'bash', '-c'])
  assert.equal(log[0][1][4], PROBE_SCRIPT); assert.deepEqual(log[0][1].slice(5), ['soak', 'a', 'b'])
  assert.deepEqual(res.inspect, { startedAt: '2026-10-28T09:00:00.123456789Z', oomKilled: false, running: true, restartCount: 0 })
  assert.equal(res.probe.http['/health'].body.sessions, 2)
  assert.ok(!PROBE_SCRIPT.includes('node '), 'the probe must not start a second Node process inside the measured cgroup')
})

test('failed docker exec becomes an error code; stderr text is never copied into the row', async () => {
  const dirPath = dir(), out = join(dirPath, 's.csv')
  openCsv(out)
  const probeFn = makeDockerProbe({ container: 'c', keys: [], exec: fakeExec([], { failExec: true }) })
  const rec = await sampleOnce({ probeFn, keys: [], source: 'container', step: 'storm', startMs: Date.now(), out })
  assert.equal(rec.error, 'docker_failed'); assert.equal(rec.rss_mb, undefined); assert.equal(rec.started_at, '2026-10-28T09:00:00.123456789Z')
  assert.ok(!readFileSync(out, 'utf8').includes('SECRET'))
  const [row] = parseCsv(readFileSync(out, 'utf8'))
  assert.equal(row.step, 'storm'); assert.equal(row.rss_mb, null); assert.equal(row.error, 'docker_failed')
})

test('a probe that throws still yields a row, and the loop keeps going', async () => {
  let n = 0
  const probeFn = async () => { if (++n === 2) throw new Error('boom'); return { probe: parseProbeOutput(CAPTURE), inspect: null, error: null } }
  const recs = []
  await runLoop({ probeFn, keys: ['a'], source: 'container', out: null, intervalMs: 1, stormIntervalMs: 1, shouldStop: () => recs.length >= 3, sleep: async () => {}, getStep: () => 'hold', onSample: (r) => recs.push(r) })
  assert.deepEqual(recs.map((r) => r.error), [undefined, 'probe_failed', undefined])
})

test('url probe: token goes only into the Authorization header, never into the row', async () => {
  const seen = []
  const fetchFn = async (url, { headers }) => { seen.push([url, headers.authorization]); return { status: 200, json: async () => (url.endsWith('/health') ? { sessions: 1, max_sessions: 2, rss_mb: 90, rss_refuse_mb: 300 } : { status: 'connected' }) } }
  const probe = makeUrlProbe({ base: 'http://x:1', token: 'TOKEN-VALUE-FOR-TEST', keys: ['a'], fetchFn })
  const rec = await sampleOnce({ probeFn: probe, keys: ['a'], source: 'url', step: 'hold', startMs: Date.now() })
  assert.deepEqual(seen.map((s) => s[0]), ['http://x:1/health', 'http://x:1/sessions/a'])
  assert.ok(seen.every((s) => s[1] === 'Bearer TOKEN-VALUE-FOR-TEST'))
  assert.ok(!JSON.stringify(rec).includes('TOKEN-VALUE'))
  assert.equal(rec.connected, 1); assert.equal(rec.error, undefined)
  const down = await sampleOnce({ probeFn: makeUrlProbe({ base: 'http://x:1', token: 't', keys: [], fetchFn: async () => { throw new Error('ECONNREFUSED') } }), keys: [], source: 'url', step: 'storm', startMs: Date.now() })
  assert.equal(down.error, 'url_unreachable')
})

test('probe script run for real with bash against a local stub: health and session lines, token only in the header', async (t) => {
  const bash = spawnSync('bash', ['-c', 'exec 3<>/dev/tcp/127.0.0.1/1 2>/dev/null; echo ok']).status
  if (bash !== 0) return t.skip('bash not available')
  const TOKEN = `test-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`
  const auth = []
  const server = http.createServer((req, res) => {
    auth.push(req.headers.authorization)
    if (req.headers.authorization !== `Bearer ${TOKEN}`) { res.writeHead(401); return res.end('{"error":"unauthorized"}') }
    if (req.url === '/health') { res.writeHead(200); return res.end('{"sessions":1,"max_sessions":2,"rss_mb":88.8,"rss_refuse_mb":300}') }
    if (req.url === '/sessions/a') { res.writeHead(200); return res.end('{"status":"connected","since_ms":1,"last_disconnect":null}') }
    res.writeHead(404); res.end('{"error":"not_found"}')
  })
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const port = server.address().port
  try {
    const go = (env) => run('bash', ['-c', PROBE_SCRIPT, 'soak', 'a', 'zz'], { env: { ...process.env, PORT: String(port), ...env } })
    const { stdout } = await go({ WHATSAPP_SIDECAR_TOKEN: TOKEN })
    const p = parseProbeOutput(stdout)
    assert.equal(p.http['/health'].status, 200); assert.equal(p.http['/health'].body.rss_mb, 88.8)
    assert.equal(p.http['/sessions/a'].body.status, 'connected'); assert.equal(p.http['/sessions/zz'].status, 404)
    assert.ok(!stdout.includes(TOKEN), 'the token must never appear in the probe output')
    assert.ok(auth.every((a) => a === `Bearer ${TOKEN}`))
    const bad = parseProbeOutput((await go({ WHATSAPP_SIDECAR_TOKEN: 'wrong' })).stdout)
    assert.equal(bad.http['/health'].status, 401)
  } finally { await new Promise((r) => { server.close(r); server.closeAllConnections() }) }
  // nothing listening: still exits 0 (a dead sidecar is a data point, not a probe failure)
  const dead = await run('bash', ['-c', PROBE_SCRIPT, 'soak', 'a'], { env: { ...process.env, PORT: String(port), WHATSAPP_SIDECAR_TOKEN: 'x' } })
  assert.deepEqual(parseProbeOutput(dead.stdout).http, {})
})

test('step file: only valid steps are read, anything else is "no step"', () => {
  const out = join(dir(), 's.csv')
  assert.equal(readStep(out), '')
  writeFileSync(stepFile(out), 'hold\n'); assert.equal(readStep(out), 'hold')
  writeFileSync(stepFile(out), 'sessions-4\n'); assert.equal(readStep(out), 'sessions-4')
  writeFileSync(stepFile(out), 'holdd\n'); assert.equal(readStep(out), '')
  writeFileSync(stepFile(out), 'hold,evil\n'); assert.equal(readStep(out), '')
})

test('loop: re-reads the step each sample, samples faster during the storm, appends rows', async () => {
  const out = join(dir(), 's.csv'); openCsv(out)
  const steps = ['hold', 'hold', 'storm', 'storm', 'hold']
  let i = 0, clock = 1_000_000
  const sleeps = []
  const probeFn = async () => ({ probe: parseProbeOutput(CAPTURE), inspect: null, error: null })
  await runLoop({ probeFn, keys: ['a'], source: 'container', out, intervalMs: 30000, stormIntervalMs: 2000, shouldStop: () => i >= steps.length, getStep: () => steps[i++], now: () => (clock += 10), sleep: async (ms) => { sleeps.push(ms) } })
  assert.deepEqual(parseCsv(readFileSync(out, 'utf8')).map((r) => r.step), steps)
  assert.deepEqual(sleeps.slice(0, 4).map((ms) => (ms > 5000 ? 'normal' : 'storm')).join(), 'normal,normal,storm,storm')
})

test('csv file: header written once, appended on resume, a foreign header is refused', () => {
  const out = join(dir(), 's.csv')
  openCsv(out); openCsv(out)
  assert.equal(readFileSync(out, 'utf8'), `${csvHeader()}\n`)
  const other = join(dir(), 'o.csv'); writeFileSync(other, 'iso_time,sessions\n')
  assert.throws(() => openCsv(other), /different header/)
})

test('CLI: mark validates the step, run/check need a target, token env is required for --url', () => {
  const out = join(dir(), 's.csv')
  const cli = (...args) => spawnSync(process.execPath, [SOAK, ...args], { encoding: 'utf8', env: { ...process.env, WHATSAPP_SIDECAR_TOKEN: '' } })
  const ok = cli('mark', 'hold', '--out', out)
  assert.equal(ok.status, 0); assert.equal(readFileSync(stepFile(out), 'utf8').trim(), 'hold')
  const bad = cli('mark', 'holdd', '--out', out)
  assert.equal(bad.status, 2); assert.match(bad.stderr, /unknown step/); assert.equal(readFileSync(stepFile(out), 'utf8').trim(), 'hold')
  assert.equal(cli('run', '--out', out).status, 2)
  const noTok = cli('check', '--url', 'http://127.0.0.1:1')
  assert.equal(noTok.status, 2); assert.match(noTok.stderr, /WHATSAPP_SIDECAR_TOKEN/)
  assert.equal(cli('check', '--container', 'a b').status, 2)
  assert.equal(cli('check', '--container', 'ok', '--keys', 'bad key').status, 2)
})
