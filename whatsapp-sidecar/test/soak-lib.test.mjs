// Soak analysis + CSV + probe parsing against SYNTHETIC CSV fixtures (generated here; no sidecar, no network, no WhatsApp).
import test from 'node:test'
import assert from 'node:assert/strict'
import { analyze, buildRecord, csvHeader, csvLine, isValidStep, linfit, median, parseCsv, parseProbeOutput, percentile, renderReport, roundUp, sessionsOfStep } from '../scripts/soak-lib.mjs'

const MIB = 1048576
const T0 = Date.parse('2026-10-28T10:00:00.000Z')

// A made-up but internally consistent run: RSS = 100 + 30/session, container memory = RSS + 40 MiB, 30 s sampling.
function scenario({ hold_h = 25, burst_at = 6, oom = false, storm = 'ok', keys = true, stormRestart = true } = {}) {
  const rows = []
  let t = T0
  const base = { source: 'container', max_sessions: 8, rss_refuse_mb: 600, cg_max_b: 384 * MIB, cg_file_b: 5 * MIB, cg_shmem_b: 0, cg_slab_reclaimable_b: 2 * MIB, cg_oom_kill: 0, cg_limit_hits: 0, started_at: '2026-10-28T09:00:00.000000000Z', oom_killed: 'false', restart_count: 0 }
  const push = (step, n, { rss, extra = {}, connected = n, dt = 30 } = {}) => {
    const r = rss ?? 100 + 30 * n
    const rec = { iso_time: new Date(t).toISOString(), elapsed_s: Math.round((t - T0) / 1000), step, sessions: n, connected: keys ? connected : null, statuses: keys ? `connected=${connected}` : null, rss_mb: r, cg_current_b: Math.round((r + 40) * MIB), cg_anon_b: Math.round(r * 0.8 * MIB), ...base, ...extra }
    rows.push(parseCsv(`${csvHeader()}\n${csvLine(rec)}`)[0])
    t += dt * 1000
  }
  for (let i = 0; i < 8; i++) push('baseline', 0, { rss: 100 + (i % 2) })
  for (const n of [1, 2, 4, 6]) {
    push('pairing', n - 1, {})                       // human pairing: must be ignored
    push('sessions-' + n, n - 1, { connected: n - 1 }) // a sample taken while the new session was still coming up: dropped by the membership filter
    for (let i = 0; i < 12; i++) push('sessions-' + n, n, { rss: 100 + 30 * n + (i % 3) * 0.2 })
  }
  for (let i = 0; i < 6; i++) push('image-burst', burst_at, { rss: 100 + 30 * 6 + (i === 3 ? 40 : 5) })
  if (oom) push('image-burst', burst_at, { extra: { cg_oom_kill: 1 } })
  const n = Math.round(hold_h * 120)
  for (let i = 0; i < n; i++) push('hold', 6, { rss: 280 + (i / 120) * 0.1 })   // +0.1 MiB per hour
  if (storm) {
    const startedNew = '2026-10-28T20:00:00.000000000Z'
    push('storm', 6, { extra: stormRestart ? { started_at: base.started_at } : {}, dt: 2 })
    // container down: docker exec fails, no metrics
    for (let i = 0; i < 3; i++) { rows.push(parseCsv(`${csvHeader()}\n${csvLine({ iso_time: new Date(t).toISOString(), source: 'container', step: 'storm', error: 'docker_failed', started_at: base.started_at })}`)[0]); t += 2000 }
    const ups = [0, 1, 3, 5, 6, 6]
    ups.forEach((c, i) => push('storm', c, { rss: 150 + i * 20, connected: c, dt: 2, extra: stormRestart ? { started_at: startedNew } : {} }))
    if (storm === 'never') { /* the ups above end at 6; replace by lowering */ rows.splice(-2, 2) }
  }
  return rows
}

test('step vocabulary', () => {
  for (const s of ['baseline', 'sessions-1', 'sessions-12', 'hold', 'image-burst', 'text-load', 'storm', 'pairing', 'transition']) assert.ok(isValidStep(s), s)
  for (const s of ['', 'sessions-0', 'sessions-', 'Hold', 'sessions-1;rm', 'sessions-1000', undefined, null]) assert.ok(!isValidStep(s), String(s))
  assert.equal(sessionsOfStep('sessions-4'), 4); assert.equal(sessionsOfStep('baseline'), 0); assert.equal(sessionsOfStep('hold'), null)
})

test('stats helpers', () => {
  assert.equal(median([3, 1, 2]), 2); assert.equal(median([4, 1, 2, 3]), 2.5); assert.equal(median([]), null); assert.equal(median([null, 5]), 5)
  assert.equal(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
  assert.equal(roundUp(500, 64), 512); assert.equal(roundUp(512, 64), 512)
  const f = linfit([[0, 100], [1, 130], [2, 160], [4, 220]])
  assert.ok(Math.abs(f.slope - 30) < 1e-9 && Math.abs(f.intercept - 100) < 1e-9 && Math.abs(f.r2 - 1) < 1e-9)
  assert.equal(linfit([[1, 5]]), null); assert.equal(linfit([[2, 5], [2, 9]]), null)
})

test('csv: header, round trip, separators in values cannot break a row, blank means null', () => {
  const rec = { iso_time: '2026-10-28T10:00:00.000Z', source: 'container', step: 'hold', sessions: 2, rss_mb: 150.5, error: 'a,b\nc', statuses: 'connected=2' }
  const line = csvLine(rec)
  assert.equal(line.split(',').length, csvHeader().split(',').length)
  const [r] = parseCsv(`${csvHeader()}\n${line}`)
  assert.equal(r.sessions, 2); assert.equal(r.rss_mb, 150.5); assert.equal(r.connected, null); assert.equal(r.error, 'a b c'); assert.equal(r.t, Date.parse(rec.iso_time))
  assert.deepEqual(parseCsv(''), [])
  // the old skeleton format still parses
  const [o] = parseCsv('iso_time,sessions,max_sessions,rss_mb\n2026-10-28T10:00:00.000Z,1,2,99.5\n')
  assert.equal(o.rss_mb, 99.5); assert.equal(o.step, undefined)
})

// Captured from the real probe script running in node:22.23.4-slim under cgroup v2 (read-only rootfs, 32m /tmp tmpfs,
// 384m limit) against a stand-in HTTP server on :8787, 2026-10-28. Key `zz` does not exist (404).
const CAPTURE = `cg current 10022912
cg max 402653184
cg anon 8278016
cg file 0
cg shmem 0
cg slab_reclaimable 124840
cg slab_unreclaimable 472680
ev max 0
ev oom 0
ev oom_kill 0
http /health 200 {"sessions":2,"max_sessions":3,"rss_mb":45.3,"rss_refuse_mb":300}
http /sessions/a 200 {"status":"connected","since_ms":5,"last_disconnect":null}
http /sessions/b 200 {"status":"reconnecting","since_ms":5,"last_disconnect":null}
http /sessions/zz 404 {"error":"not_found"}
`

test('probe output parsing + record building from a real capture', () => {
  const p = parseProbeOutput(CAPTURE)
  assert.equal(p.cg.current, 10022912); assert.equal(p.cg.max, 402653184); assert.equal(p.ev.oom_kill, 0)
  assert.equal(p.http['/health'].body.rss_mb, 45.3); assert.equal(p.http['/sessions/zz'].status, 404)
  const rec = buildRecord({ now: T0 + 5000, startMs: T0, source: 'container', step: 'hold', probe: p, keys: ['a', 'b', 'zz'], inspect: { startedAt: '2026-10-28T09:00:00Z', oomKilled: false, restartCount: 0 } })
  assert.equal(rec.sessions, 2); assert.equal(rec.connected, 1); assert.equal(rec.statuses, 'connected=1;http_404=1;reconnecting=1')
  assert.equal(rec.rss_mb, 45.3); assert.equal(rec.cg_current_b, 10022912); assert.equal(rec.cg_anon_b, 8278016); assert.equal(rec.cg_oom_kill, 0); assert.equal(rec.elapsed_s, 5)
  assert.equal(rec.error, undefined); assert.equal(rec.oom_killed, 'false')
})

test('probe parsing: garbage, limit "max", failed health and unreadable cgroup become error codes, never raw text', () => {
  const p = parseProbeOutput('cg max max\nhttp /health 401 {"error":"unauthorized"}\nsome shell noise with a secret\nhttp /sessions/a 200 {not json')
  assert.equal(p.cg.max, null); assert.equal(p.http['/sessions/a'].body, null)
  const rec = buildRecord({ now: T0, startMs: T0, source: 'container', step: 'hold', probe: p, keys: ['a'] })
  assert.equal(rec.error, 'health_401;cgroup_unreadable'); assert.equal(rec.rss_mb, undefined)
  assert.equal(rec.statuses, 'http_200=1')   // an unparsable session body is never trusted as a status
  assert.ok(!csvLine(rec).includes('secret'))
  const evil = parseProbeOutput('http /sessions/a 200 {"status":"connected,evil\\nrow"}')
  assert.equal(buildRecord({ now: T0, startMs: T0, source: 'url', step: 'x', probe: evil, keys: ['a'] }).statuses, 'http_200=1')
  assert.equal(buildRecord({ now: T0, startMs: T0, source: 'container', step: 'hold', probe: null, error: 'docker_failed' }).error, 'docker_failed')
  // url mode has no cgroup and must not report that as an error
  assert.equal(buildRecord({ now: T0, startMs: T0, source: 'url', step: 'hold', probe: parseProbeOutput('http /health 200 {"sessions":1,"max_sessions":2,"rss_mb":90,"rss_refuse_mb":300}') }).error, undefined)
})

test('analysis: baseline, marginal RSS per session, bursts, hold drift, storm, recommendation', () => {
  const r = analyze(scenario())
  assert.equal(r.synthetic, false)
  assert.equal(r.baseline.rss_median, 100.5)   // 8 samples alternating 100/101, first 25% dropped
  assert.deepEqual(r.steady.map((s) => s.sessions), [0, 1, 2, 4, 6])
  assert.ok(Math.abs(r.fit_rss.slope - 30) < 0.2, `slope ${r.fit_rss.slope}`)
  assert.ok(r.fit_rss.r2 > 0.999)
  assert.ok(Math.abs(r.fit_cg.slope - 30) < 0.2)
  // mislabelled / still-settling samples are dropped, not averaged in
  assert.equal(r.excluded_samples['sessions-4'], 1)
  assert.ok(r.steady.find((s) => s.sessions === 4).rss_max < 221)

  const b = r.bursts.find((x) => x.step === 'image-burst')
  assert.equal(b.peak_rss, 320); assert.equal(b.sessions, 6); assert.equal(b.before_step, 'sessions-6')
  assert.ok(Math.abs(b.delta_rss - 40) < 0.5)

  assert.equal(r.hold.hours, 25)   // 3000 samples, 30 s apart
  assert.ok(r.hold.at_1h.reached && r.hold.at_24h.reached)
  assert.ok(Math.abs(r.hold.slope_mb_per_h - 0.1) < 0.01)
  assert.equal(r.hold.growing, false)

  assert.equal(r.storm.expected, 6); assert.equal(r.storm.restored, true); assert.equal(r.storm.restart_seen, true)
  assert.equal(r.storm.peak_rss, 250)
  // mark at sample 0, then 3 failed rows (2 s each), then counts 0,1,3,5,6: first 6 is at index 4+... => 2*(1+3+4) s
  assert.equal(r.storm.restore_s_from_mark, 2 * (3 + 4 + 1))
  assert.equal(r.storm.weak, false)

  const rec = r.recommendation
  assert.equal(rec.valid, true); assert.equal(rec.basis_metric, 'container memory.current (cgroup)')
  assert.equal(rec.n_max_measured, 6); assert.equal(rec.max_sessions, 6)
  assert.equal(rec.measured_peak_mib, 360)                    // 320 burst peak RSS + 40 container overhead
  assert.equal(rec.mem_limit_mib, roundUp(360 * 1.5, 64))     // 540 -> 576
  assert.equal(rec.mem_limit_mib, 576)
  assert.equal(rec.rss_refuse_mb, Math.round((576 * 300) / 384))
  assert.ok(Math.abs(rec.marginal_mib_per_session - 30) < 0.2)
  const rows = Object.fromEntries(rec.table.map((t) => [t.sessions, t]))
  assert.equal(rows[6].kind, 'measured range (fit)'); assert.equal(rows[8].kind, 'EXTRAPOLATED')
  assert.ok(Math.abs(rows[8].projected_peak - 420) < 1.5)
  // 384 MiB at x1.5 leaves 256 MiB of peak: about 240 MiB at 2 sessions fits, 270 MiB at 3 does not
  assert.equal(rec.fits_today, 2)
  assert.equal(r.warnings.filter((w) => /NOT reached/.test(w)).length, 0)
})

test('analysis: short hold reports the 1 h / 24 h marks as NOT reached', () => {
  const r = analyze(scenario({ hold_h: 0.5 }))
  assert.equal(r.hold.at_1h.reached, false); assert.equal(r.hold.at_24h.reached, false)
  assert.ok(r.warnings.some((w) => /1 h and 24 h marks were NOT reached/.test(w)))
  assert.match(renderReport(r), /NOT REACHED/)
  const mid = analyze(scenario({ hold_h: 5 }))
  assert.ok(mid.hold.at_1h.reached && !mid.hold.at_24h.reached)
  assert.ok(mid.warnings.some((w) => /24 h mark was NOT reached/.test(w)))
})

test('analysis: a growing hold is flagged', () => {
  const rows = scenario({ hold_h: 6 })
  rows.filter((x) => x.step === 'hold').forEach((x, i) => { x.rss_mb = 280 + i * 0.05 })   // 6 MiB per hour
  const r = analyze(rows)
  assert.equal(r.hold.growing, true); assert.ok(r.hold.slope_mb_per_h > 5)
  assert.match(renderReport(r), /GROWING/)
})

test('analysis: kernel OOM kill invalidates the recommendation', () => {
  const r = analyze(scenario({ oom: true }))
  assert.equal(r.recommendation.valid, false); assert.match(r.recommendation.reason, /OOM-killed/)
  assert.equal(r.peaks.oom_kill, 1)
})

test('analysis: synthetic rows get a banner and no sizing', () => {
  const rows = scenario().map((x) => ({ ...x, source: 'synthetic' }))
  const r = analyze(rows)
  assert.equal(r.synthetic, true); assert.equal(r.recommendation.valid, false)
  const text = renderReport(r)
  assert.match(text, /SYNTHETIC RUN/); assert.match(text, /NONE: synthetic data/); assert.ok(!/WHATSAPP_SIDECAR_MEM_LIMIT =/.test(text))
})

test('analysis: storm without per-session statuses is flagged weak; storm that never restores says so', () => {
  const weak = analyze(scenario({ keys: false }))
  assert.equal(weak.storm.weak, true); assert.ok(weak.warnings.some((w) => /optimistic/.test(w)))
  const rows = scenario()
  const lastStorm = rows.map((x, i) => [x, i]).filter(([x]) => x.step === 'storm').map(([, i]) => i)
  rows.splice(lastStorm[lastStorm.length - 2], 2)   // drop the final two samples (5 and 6 connected is gone: top is 3 or 5)
  const never = analyze(rows)
  assert.equal(never.storm.restored, false); assert.match(renderReport(never), /NOT restored/)
})

test('analysis: storm without started_at is recognised by the outage; no outage means no restore claim', () => {
  const noStart = analyze(scenario({ stormRestart: false }).map((x) => ({ ...x, started_at: null })))
  assert.equal(noStart.storm.restored, true)
  const flat = scenario({ stormRestart: false }).map((x) => ({ ...x, started_at: null }))
  for (const x of flat) if (x.step === 'storm') { x.connected = 6; x.rss_mb = x.rss_mb ?? 280 }
  const r = analyze(flat)
  assert.equal(r.storm.restored, false); assert.ok(r.warnings.some((w) => /no restart was observed/.test(w)))
})

test('analysis: bursts at a lower session count than the top level are added as a composite and flagged', () => {
  const rows = scenario()
  const burst = rows.filter((x) => x.step === 'image-burst')
  burst.forEach((x) => { x.connected = 2; x.sessions = 2; x.rss_mb = 165; x.cg_current_b = 205 * MIB })
  burst[3].rss_mb = 200; burst[3].cg_current_b = 240 * MIB   // +40 over the 2-session level (160), compared at the same session count
  const r = analyze(rows)
  assert.equal(r.bursts[0].before_step, 'sessions-2')
  assert.ok(Math.abs(r.bursts[0].delta_rss - 40) < 0.5)
  assert.equal(r.recommendation.composite_used, true)
  assert.ok(Math.abs(r.recommendation.sizing_basis_mib - 360.4) < 0.5)   // highest steady cg_max (320.4) + 40
  assert.ok(r.warnings.some((w) => /bursts were measured at 2 sessions, below the highest level \(6\)/.test(w)))
  assert.match(renderReport(r), /COMPOSITE/)
})

test('analysis: a run censored by the capacity guard or with failed sessions says so', () => {
  const rows = scenario()
  const hold = rows.filter((x) => x.step === 'hold')
  hold[10].rss_mb = 305; hold[10].rss_refuse_mb = 300; hold[11].statuses = 'connected=5;failed=1'
  const r = analyze(rows)
  assert.ok(r.warnings.some((w) => /RSS reached RSS_REFUSE_MB \(300\) in 1 sample/.test(w)))
  assert.ok(r.warnings.some((w) => /1 sample\(s\) had a session in state "failed"/.test(w)))
  assert.equal(analyze(scenario()).warnings.filter((w) => /RSS_REFUSE_MB|"failed"/.test(w)).length, 0)
})

test('analysis: degenerate inputs never throw and say what is missing', () => {
  assert.equal(analyze([]).recommendation.valid, false)
  const only = [{ t: T0, iso_time: new Date(T0).toISOString(), source: 'url', step: 'baseline', sessions: 0, rss_mb: 90 }]
  const r = analyze(only)
  assert.equal(r.recommendation.valid, false)
  assert.ok(r.warnings.some((w) => /no cgroup columns/.test(w)))
  assert.ok(r.warnings.some((w) => /no hold step/.test(w)))
  assert.ok(typeof renderReport(r) === 'string')
  const unlabeled = analyze([{ t: T0, iso_time: new Date(T0).toISOString(), source: 'url', sessions: 1, rss_mb: 90 }])
  assert.ok(unlabeled.warnings.some((w) => /no step label/.test(w)))
})

test('report text states what is measured and what is extrapolated, and never prints an env value', () => {
  const text = renderReport(analyze(scenario()))
  for (const needle of ['[MEASURED]', '[FIT through the measured medians]', 'EXTRAPOLATED', 'WHATSAPP_SIDECAR_MEM_LIMIT = 576m', 'WHATSAPP_MAX_SESSIONS      = 6', 'Marginal cost per session']) assert.ok(text.includes(needle), needle)
  assert.ok(!/token|secret|bearer/i.test(text))
})
