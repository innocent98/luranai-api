// Reads a soak CSV (written by soak.mjs) and prints baseline, marginal RSS per session, burst peak, idle drift, restore
// time and a recommended WHATSAPP_SIDECAR_MEM_LIMIT / WHATSAPP_MAX_SESSIONS, saying which numbers are measured and which
// are extrapolated.   node scripts/soak-analyze.mjs soak.csv [--expect N] [--current-limit-mib 384] [--headroom 1.5] [--json]
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'
import { analyze, parseCsv, renderReport } from './soak-lib.mjs'

function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: { expect: { type: 'string' }, 'current-limit-mib': { type: 'string', default: '384' }, headroom: { type: 'string', default: '1.5' }, json: { type: 'boolean', default: false } },
  })
  if (positionals.length !== 1) { console.error('usage: soak-analyze.mjs soak.csv [--expect N] [--current-limit-mib 384] [--headroom 1.5] [--json]'); process.exit(2) }
  const headroom = Number(values.headroom), currentLimitMib = Number(values['current-limit-mib'])
  if (!(headroom >= 1) || !(currentLimitMib > 0)) { console.error('--headroom must be >= 1 and --current-limit-mib > 0'); process.exit(2) }
  const rows = parseCsv(readFileSync(positionals[0], 'utf8'))
  const report = analyze(rows, { headroom, currentLimitMib, expectSessions: values.expect ? Number(values.expect) : null })
  console.log(values.json ? JSON.stringify(report, (_k, v) => (v === Infinity ? 'unbounded' : v), 2) : renderReport(report))
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main()
