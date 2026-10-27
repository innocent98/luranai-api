// SKELETON: N-session RAM soak. Not run yet; this is the missing measurement behind the beta capacity decision.
//
// Intended use (needs a human with N dedicated test numbers; never point it at customer numbers):
//   1. Start the sidecar with MAX_SESSIONS=N and real env (see .env.example), watch `docker stats` / GET /health.
//   2. Pair N test numbers through the real API (POST /sessions per number), let them settle ~10 min each.
//   3. Run this script to poll GET /health on an interval and write rss_mb vs session count to a CSV.
//   4. Record: baseline RSS (0 sessions), marginal RSS per session, RSS after 1 h and 24 h, reconnect storm (restart sidecar).
//
// Usage: SIDECAR_URL=http://127.0.0.1:8787 WHATSAPP_SIDECAR_TOKEN=... node scripts/soak.mjs [intervalSeconds] [out.csv]
import { appendFileSync, writeFileSync } from 'node:fs'

const base = process.env.SIDECAR_URL || 'http://127.0.0.1:8787'
const token = process.env.WHATSAPP_SIDECAR_TOKEN
const every = Number(process.argv[2] || 30) * 1000
const out = process.argv[3] || 'soak.csv'
if (!token) { console.error('set WHATSAPP_SIDECAR_TOKEN'); process.exit(2) }

writeFileSync(out, 'iso_time,sessions,max_sessions,rss_mb\n')
const tick = async () => {
  try {
    const r = await fetch(`${base}/health`, { headers: { authorization: `Bearer ${token}` } })
    const h = await r.json()
    appendFileSync(out, `${new Date().toISOString()},${h.sessions},${h.max_sessions},${h.rss_mb}\n`)
  } catch (e) { appendFileSync(out, `${new Date().toISOString()},,,error\n`) }
}
await tick()
setInterval(tick, every)
