// SYNTHETIC soak: drives the real SessionManager + real HTTP API with the test suite's FAKE socket (no Baileys, no network,
// no Postgres/Redis) through every soak step and records the same CSV. It proves the tooling end to end and exercises the
// capacity guard; the numbers it produces are of ONE Node process holding fake sessions and are NOT representative of
// real Baileys sessions (each real session carries a websocket, Signal state and crypto buffers). soak-analyze.mjs prints a
// banner on this data and refuses to recommend limits from it.
//   node scripts/soak-synthetic.mjs [--sessions 4] [--step-seconds 3] [--interval 0.5] [--out soak-synthetic.csv]
// Imports test/ fixtures: run it from a repository checkout, not from the production image.
import { existsSync, unlinkSync } from 'node:fs'
import { parseArgs } from 'node:util'
import { pathToFileURL } from 'node:url'
import { createApi } from '../src/api.mjs'
import { ImgBuf } from '../test/image-helpers.mjs'
import { TOKEN, makeHarness, pairAndConnect, until } from '../test/helpers.mjs'
import { analyze, parseCsv, renderReport } from './soak-lib.mjs'
import { makeUrlProbe, openCsv, runLoop } from './soak.mjs'
import { readFileSync } from 'node:fs'

const CUST = '2348012345678@s.whatsapp.net'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const imageMsg = (id, bytes) => ({
  key: { id, remoteJid: CUST, fromMe: false },
  message: { imageMessage: { mimetype: 'image/jpeg', fileLength: bytes, url: 'https://mmg.whatsapp.net/o1/v/t62.x/abc', directPath: '/o1/v/t62.x/abc?ccb=1', mediaKey: Buffer.alloc(32, 5) } },
  pushName: 'Ada', messageTimestamp: 1_800_000_000,
})

export async function runSynthetic({ sessions = 4, stepSeconds = 3, intervalSeconds = 0.5, out, log = () => {} }) {
  const stepMs = stepSeconds * 1000
  const keys = Array.from({ length: sessions }, (_, i) => `synth${i + 1}`)
  const mkHarness = (over) => {
    const h = makeHarness({ ...over, cfg: { maxSessions: sessions + 2, restoreStaggerMs: 0, ...over?.cfg } })
    h.deps.rssBytes = () => process.memoryUsage().rss   // real RSS of this one process instead of the harness's fixed number
    return h
  }
  let cur = mkHarness()
  // The API outlives the simulated restart, so it talks to whichever manager is current.
  const manager = new Proxy({}, { get: (_t, p) => { const v = cur.mgr[p]; return typeof v === 'function' ? v.bind(cur.mgr) : v } })
  const server = createApi({ manager, token: TOKEN, log: cur.logCap.log })
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const base = `http://127.0.0.1:${server.address().port}`

  if (existsSync(out)) unlinkSync(out)
  openCsv(out)
  let step = 'baseline', stop = false, samples = 0
  const loop = runLoop({
    probeFn: makeUrlProbe({ base, token: TOKEN, keys }), keys, source: 'synthetic', out,
    intervalMs: intervalSeconds * 1000, stormIntervalMs: Math.min(intervalSeconds * 1000, 500),
    shouldStop: () => stop, getStep: () => step, onSample: () => { samples++ },
  })
  const phase = async (name) => { step = name; log(`step ${name}`); await sleep(stepMs) }

  try {
    await phase('baseline')
    let paired = 0
    for (const n of [...new Set([1, 2, 4, 6, sessions])].filter((x) => x <= sessions).sort((a, b) => a - b)) {
      step = 'pairing'
      while (paired < n) { await pairAndConnect(cur, keys[paired], `23480000${String(paired).padStart(5, '0')}`); paired++ }
      await phase(`sessions-${n}`)
    }

    // Image burst: every session receives a few inbound "photos" at once (1 MiB each, through the real download/store path).
    step = 'image-burst'; log('step image-burst')
    let id = 0
    for (let round = 0; round < 3; round++) {
      const sent = []
      for (let i = 0; i < sessions; i++) {
        const mid = `SYN${++id}`
        cur.mediaFor[mid] = ImgBuf('image/jpeg', 1048576)
        cur.mgr.get(keys[i]).sock.upsert([imageMsg(mid, 1048576)]); sent.push(mid)
      }
      await until(() => sent.every((m) => cur.emitted.some((e) => e.type === 'message.inbound' && e.message_id === m)), 5000)
      await sleep(stepMs / 3)
    }
    await phase('hold')

    // Reconnect storm: stop the manager (SIGTERM path), start a fresh one on the same stores, reopen sockets as they appear.
    step = 'storm'; log('step storm')
    await cur.mgr.shutdown()
    const next = mkHarness({ db: cur.db, leaseBackend: cur.leaseBackend })
    const opener = setInterval(() => { for (const s of next.sockets) if (!s.__opened) { s.__opened = true; s.open() } }, 20)
    cur = next
    const seen = samples
    await until(() => samples >= seen + 2, 5000)   // hold the outage open until the collector has sampled it, however slow the machine
    await cur.mgr.restoreAll()
    await until(() => keys.every((k) => cur.mgr.sessions.get(k)?.status === 'connected'), 10000)
    await sleep(stepMs)
    clearInterval(opener)
  } finally {
    stop = true
    await loop
    await cur.mgr.shutdown().catch(() => {})
    await new Promise((r) => { server.close(r); server.closeAllConnections() })
  }
  return analyze(parseCsv(readFileSync(out, 'utf8')))
}

async function main() {
  const { values } = parseArgs({ options: { sessions: { type: 'string', default: '4' }, 'step-seconds': { type: 'string', default: '3' }, interval: { type: 'string', default: '0.5' }, out: { type: 'string', default: 'soak-synthetic.csv' } } })
  const report = await runSynthetic({ sessions: Number(values.sessions), stepSeconds: Number(values['step-seconds']), intervalSeconds: Number(values.interval), out: values.out, log: (m) => console.log(m) })
  console.log(`\nwrote ${values.out}\n`)
  console.log(renderReport(report))
  process.exit(0)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch((e) => { console.error(`soak-synthetic: ${e.message}`); process.exit(1) })
