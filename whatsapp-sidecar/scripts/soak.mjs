// N-session RAM soak collector. Needs a human holding N dedicated TEST phones; never point it at customer numbers.
// Writes one CSV row per interval: sidecar GET /health + the container's cgroup memory + a step marker.
// Runbook, exact commands and how to read the result: docs/sop/whatsapp_sidecar_soak.md
//
//   node scripts/soak.mjs check --container luran_staging_whatsapp_sidecar --keys a,b      one sample to stdout, writes nothing
//   node scripts/soak.mjs run   --container luran_staging_whatsapp_sidecar --keys a,b --out soak.csv [--interval 30] [--storm-interval 2]
//   node scripts/soak.mjs mark  hold --out soak.csv                                          label what the phones are doing now
//   node scripts/soak-analyze.mjs soak.csv
//
// Container mode runs ONE `docker exec` per sample (bash + cat only, no second Node process): /health and the per-session
// status are fetched from inside the container with the token the container already has, so the token is never on the
// operator's shell, in argv or in the CSV. Direct mode (--url, token from env WHATSAPP_SIDECAR_TOKEN) is for a sidecar
// you can reach over HTTP (a local run, the synthetic mode); it has no cgroup columns.
import { execFile } from 'node:child_process'
import { appendFileSync, existsSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { parseArgs, promisify } from 'node:util'
import { COLUMNS, FIXED_STEPS, buildRecord, csvHeader, csvLine, isValidStep, parseProbeOutput } from './soak-lib.mjs'

const run = promisify(execFile)
const KEY_RE = /^[A-Za-z0-9_-]{1,64}$/
const CONTAINER_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/

// Printed lines only (see parseProbeOutput). The token is expanded by the bash builtin printf inside the container,
// so it is never an argv element of any process. Keys arrive as positional parameters, never interpolated into the text.
export const PROBE_SCRIPT = String.raw`
cg=/sys/fs/cgroup
for f in current max; do v=$(cat $cg/memory.$f 2>/dev/null) && echo "cg $f $v"; done
grep -E '^(anon|file|shmem|slab_reclaimable|slab_unreclaimable) ' $cg/memory.stat 2>/dev/null | sed 's/^/cg /'
grep -E '^(max|oom|oom_kill) ' $cg/memory.events 2>/dev/null | sed 's/^/ev /'
get() {
  exec 3<>"/dev/tcp/127.0.0.1/\${PORT:-8787}" || return
  printf 'GET %s HTTP/1.0\r\nHost: sidecar\r\nAuthorization: Bearer %s\r\nConnection: close\r\n\r\n' "$1" "$WHATSAPP_SIDECAR_TOKEN" >&3
  resp=$(cat <&3)
  exec 3<&-
  code=\${resp:9:3}
  body=\${resp#*$'\r\n\r\n'}
  printf 'http %s %s %s\n' "$1" "$code" "\${body//$'\n'/ }"
}
get /health
for k in "$@"; do get "/sessions/$k"; done
exit 0
`.replace(/\\\$/g, '$')

const failCode = (e) => (e && e.killed ? 'docker_timeout' : 'docker_failed')

export function makeDockerProbe({ container, keys, exec = run, dockerBin = 'docker' }) {
  return async () => {
    let probe = null, inspect = null, error = null
    try {
      const { stdout } = await exec(dockerBin, ['exec', container, 'bash', '-c', PROBE_SCRIPT, 'soak', ...keys], { timeout: 20000, maxBuffer: 1 << 20 })
      probe = parseProbeOutput(stdout)
    } catch (e) { error = failCode(e) }   // stderr is not copied: it could echo anything
    try {
      const { stdout } = await exec(dockerBin, ['inspect', '-f', '{{.State.StartedAt}}|{{.State.OOMKilled}}|{{.State.Running}}|{{.RestartCount}}', container], { timeout: 10000 })
      const [startedAt, oomKilled, running, restartCount] = stdout.trim().split('|')
      inspect = { startedAt, oomKilled: oomKilled === 'true', running: running === 'true', restartCount: Number(restartCount) }
    } catch { error = error ? `${error};inspect_failed` : 'inspect_failed' }
    return { probe, inspect, error }
  }
}

export function makeUrlProbe({ base, token, keys, fetchFn = fetch }) {
  const get = async (path) => {
    const r = await fetchFn(`${base}${path}`, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(10000) })
    let body = null
    try { body = await r.json() } catch { /* status is enough */ }
    return { status: r.status, body }
  }
  return async () => {
    const http = {}
    try {
      http['/health'] = await get('/health')
      for (const k of keys) http[`/sessions/${k}`] = await get(`/sessions/${k}`)
    } catch { return { probe: { cg: {}, ev: {}, http }, inspect: null, error: 'url_unreachable' } }
    return { probe: { cg: {}, ev: {}, http }, inspect: null, error: null }
  }
}

export const stepFile = (out) => `${out}.step`
export function readStep(out) {
  try { const s = readFileSync(stepFile(out), 'utf8').trim(); return isValidStep(s) ? s : '' } catch { return '' }
}

// One sample -> one CSV line, appended. Never throws: a failed sample is a row with an `error` code and empty metrics.
export async function sampleOnce({ probeFn, keys, source, step, startMs, now = Date.now, out }) {
  let res
  try { res = await probeFn() } catch { res = { probe: null, inspect: null, error: 'probe_failed' } }
  const rec = buildRecord({ now: now(), startMs, source, step, probe: res.probe, keys, inspect: res.inspect, error: res.error })
  if (out) appendFileSync(out, `${csvLine(rec)}\n`)
  return rec
}

export async function runLoop({ probeFn, keys, source, out, intervalMs, stormIntervalMs, shouldStop = () => false, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), now = Date.now, getStep = () => readStep(out), onSample = () => {} }) {
  const startMs = now()
  while (!shouldStop()) {
    const t0 = now()
    const step = getStep()
    const rec = await sampleOnce({ probeFn, keys, source, step, startMs, now, out })
    onSample(rec)
    if (shouldStop()) break
    const every = step === 'storm' ? stormIntervalMs : intervalMs
    await sleep(Math.max(0, every - (now() - t0)))
  }
}

export function openCsv(out) {
  if (!existsSync(out) || statSync(out).size === 0) { writeFileSync(out, `${csvHeader()}\n`); return }
  const first = readFileSync(out, 'utf8').split('\n', 1)[0].trim()
  if (first !== csvHeader()) throw new Error(`${out} exists with a different header; refusing to append (pick a new --out)`)
}

const OPTIONS = {
  container: { type: 'string' }, url: { type: 'string' }, keys: { type: 'string', default: '' }, out: { type: 'string' },
  interval: { type: 'string', default: '30' }, 'storm-interval': { type: 'string', default: '2' },
}

function usage(msg) {
  if (msg) console.error(msg)
  console.error('usage: soak.mjs run|check --container NAME | --url URL [--keys a,b,c] --out FILE [--interval S] [--storm-interval S]\n       soak.mjs mark <step> --out FILE\n' +
    `steps: ${FIXED_STEPS.join(', ')}, sessions-<N>`)
  process.exit(2)
}

function buildProbe(values) {
  const keys = values.keys ? values.keys.split(',').map((k) => k.trim()).filter(Boolean) : []
  for (const k of keys) if (!KEY_RE.test(k)) usage('--keys: session keys are [A-Za-z0-9_-]{1,64}')
  if (values.container && values.url) usage('use --container or --url, not both')
  if (values.container) {
    if (!CONTAINER_RE.test(values.container)) usage('--container: not a container name')
    return { keys, source: 'container', probeFn: makeDockerProbe({ container: values.container, keys }) }
  }
  if (values.url) {
    const token = process.env.WHATSAPP_SIDECAR_TOKEN
    if (!token) usage('--url mode reads the token from env WHATSAPP_SIDECAR_TOKEN (set it; it is never printed)')
    return { keys, source: 'url', probeFn: makeUrlProbe({ base: values.url.replace(/\/+$/, ''), token, keys }) }
  }
  return usage('need --container NAME or --url URL')
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2)
  if (cmd === 'mark') {
    const { values, positionals } = parseArgs({ args: rest, options: OPTIONS, allowPositionals: true })
    const step = positionals[0]
    if (!isValidStep(step)) usage(`unknown step ${JSON.stringify(step)}`)
    if (!values.out) usage('--out FILE is required (the collector reads <out>.step)')
    writeFileSync(stepFile(values.out), `${step}\n`)
    console.log(`${new Date().toISOString()} step -> ${step}`)
    return
  }
  if (cmd !== 'run' && cmd !== 'check') usage(cmd ? `unknown command ${cmd}` : '')
  const { values } = parseArgs({ args: rest, options: OPTIONS })
  const intervalMs = Number(values.interval) * 1000, stormIntervalMs = Number(values['storm-interval']) * 1000
  if (!(intervalMs >= 1000) || !(stormIntervalMs >= 500)) usage('--interval must be >= 1 s, --storm-interval >= 0.5 s')
  const { keys, source, probeFn } = buildProbe(values)

  if (cmd === 'check') {
    const rec = await sampleOnce({ probeFn, keys, source, step: 'check', startMs: Date.now() })
    for (const c of COLUMNS) if (rec[c] !== undefined && rec[c] !== null) console.log(`${c}=${rec[c]}`)
    if (rec.error) { console.error(`sample had errors: ${rec.error}`); process.exit(1) }
    if (!keys.length) console.log('note: no --keys, so per-session status (and a trustworthy storm restore time) is not recorded')
    return
  }

  if (!values.out) usage('--out FILE is required')
  openCsv(values.out)
  let stop = false
  for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => { stop = true })
  console.log(`soak: ${source} sampling every ${values.interval}s (${values['storm-interval']}s during step storm) -> ${values.out}; step file ${stepFile(values.out)}; Ctrl-C to stop`)
  let n = 0
  await runLoop({
    probeFn, keys, source, out: values.out, intervalMs, stormIntervalMs, shouldStop: () => stop,
    onSample: (r) => { if (++n % 20 === 1 || r.error) console.log(`${r.iso_time} step=${r.step || '(none)'} sessions=${r.sessions ?? '-'} connected=${r.connected ?? '-'} rss=${r.rss_mb ?? '-'}MiB${r.cg_current_b ? ` container=${(r.cg_current_b / 1048576).toFixed(1)}MiB` : ''}${r.error ? ` ERROR=${r.error}` : ''}`) },
  })
  console.log('soak: stopped')
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch((e) => { console.error(`soak: ${e.message}`); process.exit(1) })
