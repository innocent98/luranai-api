// Pure logic for the N-session RAM soak: CSV schema, probe-output parsing, step vocabulary, statistics and the analysis
// that turns a CSV into sizing numbers. No I/O and no dependencies, so it is unit tested with synthetic CSV fixtures
// (test/soak-lib.test.mjs). Collector: soak.mjs. Report: soak-analyze.mjs. Runbook: docs/sop/whatsapp_sidecar_soak.md.

const MIB = 1048576

export const COLUMNS = [
  'iso_time', 'elapsed_s', 'source', 'step',
  'sessions', 'max_sessions', 'connected', 'statuses', 'rss_mb', 'rss_refuse_mb',
  'cg_current_b', 'cg_max_b', 'cg_anon_b', 'cg_file_b', 'cg_shmem_b', 'cg_slab_reclaimable_b', 'cg_oom_kill', 'cg_limit_hits',
  'started_at', 'oom_killed', 'restart_count', 'error',
]
const TEXT_COLUMNS = new Set(['iso_time', 'source', 'step', 'statuses', 'started_at', 'oom_killed', 'error'])

// ---------------------------------------------------------------- steps
// `pairing` and `transition` are excluded from every statistic: they are for the minutes in which a human is pairing a
// phone or restarting things, when the numbers mean nothing.
export const FIXED_STEPS = ['baseline', 'text-load', 'image-burst', 'hold', 'storm', 'pairing', 'transition']
export const BURST_STEPS = ['text-load', 'image-burst']
export const IGNORED_STEPS = ['pairing', 'transition', '']
export const isValidStep = (s) => typeof s === 'string' && (FIXED_STEPS.includes(s) || /^sessions-[1-9]\d{0,2}$/.test(s))
export const sessionsOfStep = (s) => { const m = /^sessions-(\d+)$/.exec(s || ''); return m ? Number(m[1]) : s === 'baseline' ? 0 : null }

// ---------------------------------------------------------------- CSV
const cell = (v) => (v === null || v === undefined ? '' : String(v).replace(/[,\r\n]/g, ' '))
export const csvHeader = () => COLUMNS.join(',')
export const csvLine = (rec) => COLUMNS.map((c) => cell(rec[c])).join(',')

export function parseCsv(text) {
  const lines = String(text).split(/\r?\n/).filter((l) => l.trim() !== '')
  if (!lines.length) return []
  const head = lines[0].split(',').map((h) => h.trim())
  return lines.slice(1).map((l) => {
    const f = l.split(',')
    const r = {}
    head.forEach((h, i) => {
      const v = (f[i] ?? '').trim()
      r[h] = v === '' ? null : TEXT_COLUMNS.has(h) ? v : Number.isFinite(Number(v)) ? Number(v) : v
    })
    r.t = Date.parse(r.iso_time)
    return r
  }).filter((r) => Number.isFinite(r.t))
}

// ---------------------------------------------------------------- probe output
// The in-container probe (soak.mjs PROBE_SCRIPT) prints one line per fact:
//   cg <name> <integer|max>            cgroup v2 memory.current / memory.max / memory.stat fields
//   ev <name> <integer>                memory.events counters
//   http <path> <status> <json body>   /health and /sessions/<key>
// Nothing else is accepted, and no value that is not a plain number or a known status string is ever kept.
export function parseProbeOutput(text) {
  const cg = {}, ev = {}, http = {}
  for (const line of String(text).split(/\r?\n/)) {
    let m
    if ((m = /^cg (\w+) (\d+|max)$/.exec(line))) cg[m[1]] = m[2] === 'max' ? null : Number(m[2])
    else if ((m = /^ev (\w+) (\d+)$/.exec(line))) ev[m[1]] = Number(m[2])
    else if ((m = /^http (\/[\w/-]*) (\d{3}) (.*)$/.exec(line))) {
      let body = null
      try { body = JSON.parse(m[3]) } catch { /* reported as a parse error by the caller */ }
      http[m[1]] = { status: Number(m[2]), body }
    }
  }
  return { cg, ev, http }
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

// probe: parseProbeOutput result (or null); keys: session keys polled; inspect: { startedAt, oomKilled, running, restartCount } | null
export function buildRecord({ now, startMs, source, step, probe, keys = [], inspect = null, error = null }) {
  const rec = { iso_time: new Date(now).toISOString(), elapsed_s: Math.round((now - startMs) / 1000), source, step: step || '' }
  const errs = error ? [error] : []
  if (probe) {
    const h = probe.http['/health']
    if (!h) { if (!error) errs.push('no_health') }
    else if (h.status !== 200 || !h.body) errs.push(`health_${h.status}`)
    else {
      Object.assign(rec, { sessions: num(h.body.sessions), max_sessions: num(h.body.max_sessions), rss_mb: num(h.body.rss_mb), rss_refuse_mb: num(h.body.rss_refuse_mb) })
    }
    if (keys.length) {
      const counts = {}
      for (const k of keys) {
        const r = probe.http[`/sessions/${k}`]
        const st = r && r.status === 200 && r.body && typeof r.body.status === 'string' && /^[a-z_]{1,24}$/.test(r.body.status) ? r.body.status : r ? `http_${r.status}` : 'unreachable'
        counts[st] = (counts[st] || 0) + 1
      }
      rec.connected = counts.connected || 0
      rec.statuses = Object.entries(counts).sort().map(([k, v]) => `${k}=${v}`).join(';')
    }
    const { cg, ev } = probe
    if (source !== 'container') { /* url / synthetic: no container cgroup to read */ }
    else if (cg.current === undefined) errs.push('cgroup_unreadable')
    else Object.assign(rec, {
      cg_current_b: num(cg.current), cg_max_b: num(cg.max), cg_anon_b: num(cg.anon), cg_file_b: num(cg.file), cg_shmem_b: num(cg.shmem),
      cg_slab_reclaimable_b: num(cg.slab_reclaimable), cg_oom_kill: num(ev.oom_kill), cg_limit_hits: num(ev.max),
    })
  }
  if (inspect) Object.assign(rec, { started_at: inspect.startedAt || null, oom_killed: inspect.oomKilled == null ? null : String(inspect.oomKilled), restart_count: num(inspect.restartCount) })
  if (errs.length) rec.error = errs.join(';')
  return rec
}

// ---------------------------------------------------------------- statistics
export const median = (xs) => {
  const a = xs.filter((x) => x !== null && x !== undefined && Number.isFinite(x)).sort((p, q) => p - q)
  if (!a.length) return null
  const m = a.length >> 1
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2
}
export const maxOf = (xs) => { const a = xs.filter((x) => x !== null && x !== undefined && Number.isFinite(x)); return a.length ? Math.max(...a) : null }
export function percentile(xs, p) {
  const a = xs.filter((x) => x !== null && Number.isFinite(x)).sort((q, r) => q - r)
  if (!a.length) return null
  return a[Math.min(a.length - 1, Math.ceil((p / 100) * a.length) - 1)]
}

// Ordinary least squares y = intercept + slope * x. Needs two distinct x values.
export function linfit(points) {
  const p = points.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
  if (p.length < 2 || new Set(p.map(([x]) => x)).size < 2) return null
  const n = p.length
  const mx = p.reduce((s, [x]) => s + x, 0) / n, my = p.reduce((s, [, y]) => s + y, 0) / n
  let sxx = 0, sxy = 0, syy = 0
  for (const [x, y] of p) { sxx += (x - mx) ** 2; sxy += (x - mx) * (y - my); syy += (y - my) ** 2 }
  const slope = sxy / sxx
  return { slope, intercept: my - slope * mx, r2: syy === 0 ? 1 : (sxy * sxy) / (sxx * syy), n }
}

export const roundUp = (x, step) => Math.ceil(x / step) * step
const mib = (b) => (b === null || b === undefined ? null : b / MIB)
const r1 = (x) => (x === null || x === undefined ? null : Math.round(x * 10) / 10)

// ---------------------------------------------------------------- analysis
const countOf = (r) => (r.connected ?? r.sessions)
const rssOf = (r) => r.rss_mb
const cgOf = (r) => mib(r.cg_current_b)

// Drop the first `frac` of a step's samples (sessions are still settling right after a human marks the step).
function settled(samples, frac) {
  if (samples.length < 4) return samples
  return samples.slice(Math.ceil(samples.length * frac))
}

function levelOf(samples) {
  return {
    samples: samples.length,
    rss_median: r1(median(samples.map(rssOf))), rss_p95: r1(percentile(samples.map(rssOf), 95)), rss_max: r1(maxOf(samples.map(rssOf))),
    cg_median: r1(median(samples.map(cgOf))), cg_max: r1(maxOf(samples.map(cgOf))),
    anon_median: r1(median(samples.map((r) => mib(r.cg_anon_b)))),
  }
}

// opts.expectSessions overrides the pre-storm session count; opts.currentLimitMib is the limit in force during the run's
// question "how many sessions fit today"; opts.headroom is the multiplier on the measured peak (1.5 = measured + 50 %).
export function analyze(allRows, opts = {}) {
  const { settleFrac = 0.25, headroom = 1.5, currentLimitMib = 384, limitStepMib = 64, expectSessions = null } = opts
  const rows = allRows.slice().sort((a, b) => a.t - b.t)
  const warnings = []
  const sources = [...new Set(rows.map((r) => r.source).filter(Boolean))]
  const synthetic = sources.includes('synthetic')
  const ok = rows.filter((r) => rssOf(r) !== null && rssOf(r) !== undefined)
  const errorRows = rows.length - ok.length
  const report = { rows: rows.length, ok_rows: ok.length, error_rows: errorRows, sources, synthetic, warnings }
  if (synthetic) warnings.push('SYNTHETIC DATA: produced by soak-synthetic.mjs against the fake socket. NOT representative of real Baileys sessions. No sizing is produced.')
  if (!ok.length) { warnings.push('no usable samples (every row failed)'); report.recommendation = { valid: false, reason: 'no samples' }; return report }
  report.hasCgroup = ok.some((r) => cgOf(r) !== null)
  if (!report.hasCgroup && !synthetic) warnings.push('no cgroup columns: container memory (tmpfs, page cache, transient processes) is NOT measured, limits below are understated')
  report.span_h = r1((ok[ok.length - 1].t - ok[0].t) / 3600000)

  // ---- group by step, in order of first appearance
  const order = []
  const bySteps = new Map()
  for (const r of ok) {
    const s = r.step || ''
    if (!bySteps.has(s)) { bySteps.set(s, []); order.push(s) }
    bySteps.get(s).push(r)
  }
  const unlabeled = bySteps.has('') ? bySteps.get('').length : 0
  if (unlabeled) warnings.push(`${unlabeled} sample(s) had no step label and were ignored (run \`soak.mjs mark <step>\` before each phase)`)
  const usable = order.filter((s) => !IGNORED_STEPS.includes(s))
  const unknown = usable.filter((s) => !isValidStep(s))
  if (unknown.length) warnings.push(`unknown step labels ignored: ${unknown.join(', ')}`)

  // ---- steady levels: baseline (0 sessions) and sessions-N
  const steady = []
  const excluded = {}
  for (const s of order) {
    const n = sessionsOfStep(s)
    if (n === null) continue
    const all = bySteps.get(s)
    const members = all.filter((r) => countOf(r) === n)   // a sample taken while a session was still coming up is not at level N
    if (members.length !== all.length) excluded[s] = all.length - members.length
    const lv = levelOf(settled(members, settleFrac))
    if (!lv.samples) { warnings.push(`step ${s}: no sample ever showed ${n} ${s === 'baseline' ? 'sessions' : 'connected sessions'}`); continue }
    steady.push({ step: s, sessions: n, ...lv })
  }
  steady.sort((a, b) => a.sessions - b.sessions)
  report.steady = steady
  report.excluded_samples = excluded
  for (const [s, k] of Object.entries(excluded)) warnings.push(`step ${s}: ${k} sample(s) dropped because the session count did not match the label`)
  const baseline = steady.find((x) => x.sessions === 0) || null
  report.baseline = baseline
  if (!baseline) warnings.push('no baseline (0 sessions) level: marginal cost per session has no anchor')
  if (steady.length && !steady.some((x) => x.sessions >= 1)) warnings.push('no sessions-N level recorded')

  // ---- marginal cost per session
  report.fit_rss = linfit(steady.map((x) => [x.sessions, x.rss_median]))
  report.fit_cg = report.hasCgroup ? linfit(steady.map((x) => [x.sessions, x.cg_median])) : null
  if (!report.fit_rss) warnings.push('marginal RSS per session not computed: need levels at two or more different session counts')
  else if (steady.length < 3) warnings.push('marginal RSS per session is a fit through only 2 levels: no linearity check')

  // ---- bursts
  const burstLevelBefore = (step) => {
    const idx = order.indexOf(step)
    for (let i = idx - 1; i >= 0; i--) {
      if (IGNORED_STEPS.includes(order[i]) || BURST_STEPS.includes(order[i])) continue
      const st = steady.find((x) => x.step === order[i])
      if (st) return st
      const rows2 = bySteps.get(order[i])
      return { step: order[i], ...levelOf(settled(rows2, settleFrac)) }
    }
    return null
  }
  report.bursts = BURST_STEPS.filter((s) => bySteps.has(s)).map((s) => {
    const rs = bySteps.get(s)
    // Compare with the steady level at the SAME session count when there is one; otherwise with the step just before.
    const before = steady.find((x) => x.sessions === median(rs.map(countOf))) || burstLevelBefore(s)
    const peakRss = maxOf(rs.map(rssOf)), peakCg = maxOf(rs.map(cgOf))
    return {
      step: s, samples: rs.length, sessions: median(rs.map(countOf)),
      peak_rss: r1(peakRss), peak_cg: r1(peakCg), peak_shmem: r1(maxOf(rs.map((r) => mib(r.cg_shmem_b)))),
      before_step: before ? before.step : null, before_rss: before ? before.rss_median : null, before_cg: before ? before.cg_median : null,
      delta_rss: before && before.rss_median !== null ? r1(peakRss - before.rss_median) : null,
      delta_cg: before && before.cg_median !== null && peakCg !== null ? r1(peakCg - before.cg_median) : null,
    }
  })

  // ---- hold (idle drift)
  report.hold = null
  if (bySteps.has('hold')) {
    const hs = bySteps.get('hold')
    const t0 = hs[0].t
    const gaps = hs.slice(1).map((r, i) => r.t - hs[i].t)
    const tol = Math.max(2 * (median(gaps) || 0), 300000)
    const at = (targetMs) => {
      if (hs[hs.length - 1].t - t0 < targetMs - tol) return { reached: false }
      const w = hs.filter((r) => Math.abs(r.t - t0 - targetMs) <= tol)
      return w.length ? { reached: true, rss: r1(median(w.map(rssOf))), cg: r1(median(w.map(cgOf))) } : { reached: false }
    }
    const k = Math.min(5, hs.length)
    const startRss = median(hs.slice(0, k).map(rssOf)), endRss = median(hs.slice(-k).map(rssOf))
    const fit = linfit(hs.map((r) => [(r.t - t0) / 3600000, rssOf(r)]))
    const hours = (hs[hs.length - 1].t - t0) / 3600000
    const total = fit ? fit.slope * hours : null
    report.hold = {
      samples: hs.length, hours: r1(hours), sessions: median(hs.map(countOf)),
      start_rss: r1(startRss), end_rss: r1(endRss), drift_mb: r1(endRss - startRss),
      slope_mb_per_h: fit ? Math.round(fit.slope * 100) / 100 : null, r2: fit ? Math.round(fit.r2 * 100) / 100 : null,
      at_1h: at(3600000), at_24h: at(86400000),
      cg_start: r1(median(hs.slice(0, k).map(cgOf))), cg_end: r1(median(hs.slice(-k).map(cgOf))),
      slab_start_mb: r1(median(hs.slice(0, k).map((r) => mib(r.cg_slab_reclaimable_b)))), slab_end_mb: r1(median(hs.slice(-k).map((r) => mib(r.cg_slab_reclaimable_b)))),
      growing: !!fit && total > Math.max(10, 0.05 * startRss) && fit.slope > 0 && fit.r2 >= 0.5,
    }
    if (hours < 1) warnings.push(`hold ran ${r1(hours)} h: the 1 h and 24 h marks were NOT reached`)
    else if (hours < 24) warnings.push(`hold ran ${r1(hours)} h: the 24 h mark was NOT reached`)
  } else warnings.push('no hold step: idle drift not measured')

  // ---- reconnect storm
  report.storm = null
  if (bySteps.has('storm')) {
    const all = rows   // include error rows: the container is down for part of it
    const ss = all.filter((r) => r.step === 'storm')
    const stormStart = ss[0].t
    const before = ok.filter((r) => r.t < stormStart && !IGNORED_STEPS.includes(r.step || ''))
    const prior = before.length ? before[before.length - 1] : null
    const startedBefore = (prior && prior.started_at) || (ss.find((r) => r.started_at) || {}).started_at || null
    const fresh = (r) => (startedBefore && r.started_at ? r.started_at !== startedBefore : null)
    const afterRestart = ss.filter((r) => fresh(r) === true)
    const expected = expectSessions ?? (prior ? countOf(prior) : null)
    const weak = ss.every((r) => r.connected === null || r.connected === undefined)
    const haveStart = ss.some((r) => fresh(r) !== null)
    // Without started_at (url mode) the restart is recognised by the outage itself: the first failed sample or the first
    // one with fewer sessions than expected. A storm with no dip proves nothing (the restart may not have happened).
    const dip = haveStart || expected === null ? -1 : ss.findIndex((r) => rssOf(r) === null || (weak ? r.sessions : r.connected) < expected)
    const post = haveStart ? afterRestart : dip >= 0 ? ss.slice(dip) : expected === null ? ss : []
    const restored = expected === null ? null : post.find((r) => rssOf(r) !== null && (weak ? r.sessions : r.connected) >= expected)
    const startedMs = afterRestart.length ? Date.parse(afterRestart[0].started_at.replace(/(\.\d{3})\d+/, '$1')) : null
    const okPost = post.filter((r) => rssOf(r) !== null)
    report.storm = {
      expected, weak, restart_seen: afterRestart.length > 0,
      restored: !!restored,
      restore_s_from_mark: restored ? r1((restored.t - stormStart) / 1000) : null,
      restore_s_from_container_start: restored && Number.isFinite(startedMs) ? r1((restored.t - startedMs) / 1000) : null,
      last_connected: okPost.length ? okPost[okPost.length - 1].connected ?? okPost[okPost.length - 1].sessions : null,
      peak_rss: r1(maxOf(okPost.map(rssOf))), peak_cg: r1(maxOf(okPost.map(cgOf))),
      samples: ss.length, error_rows: ss.length - ss.filter((r) => rssOf(r) !== null).length,
      resolution_s: r1((median(ss.slice(1).map((r, i) => r.t - ss[i].t)) || 0) / 1000),
    }
    if (!haveStart && expected !== null && dip < 0) warnings.push('storm: sessions never dropped below the expected count, so no restart was observed (sample interval too long, or the restart did not happen)')
    if (!afterRestart.length && startedBefore) warnings.push('storm: no restart seen in started_at: was the sidecar restarted after `mark storm`?')
    if (expected === null) warnings.push('storm: could not tell how many sessions to wait for (no step before it); pass --expect N')
    if (weak) warnings.push('storm: no per-session statuses (collector ran without --keys), so "restored" counts sessions in ANY non-logged_out state, including connecting. Restore time is optimistic')
  } else warnings.push('no storm step: reconnect storm not measured')

  // ---- the capacity guard censors a soak: it refuses sessions / skips restores once RSS reaches RSS_REFUSE_MB
  const guarded = ok.filter((r) => r.rss_refuse_mb && r.rss_mb >= r.rss_refuse_mb)
  if (guarded.length) warnings.push(`RSS reached RSS_REFUSE_MB (${guarded[0].rss_refuse_mb}) in ${guarded.length} sample(s): the capacity guard was active, so new sessions and restores were refused. Raise WHATSAPP_RSS_REFUSE_MB for the soak or the run is censored`)
  const failed = rows.filter((r) => /(^|;)failed=/.test(r.statuses || ''))
  if (failed.length) warnings.push(`${failed.length} sample(s) had a session in state "failed" (first at ${failed[0].iso_time}): capacity guard 503, a restricted number (403) or undecryptable credentials. The run does not hold the intended session count`)

  // ---- whole-run peaks and safety counters
  report.peaks = {
    rss: r1(maxOf(ok.map(rssOf))), cg: r1(maxOf(ok.map(cgOf))),
    oom_kill: maxOf(rows.map((r) => r.cg_oom_kill)), limit_hits: maxOf(rows.map((r) => r.cg_limit_hits)),
    oom_killed_flag: rows.some((r) => r.oom_killed === 'true'),
    restarts_seen: new Set(rows.map((r) => r.started_at).filter(Boolean)).size,
  }
  const limitSeen = maxOf(rows.map((r) => mib(r.cg_max_b)))
  report.limit_mib_in_force = r1(limitSeen)
  const oom = (report.peaks.oom_kill || 0) > 0 || report.peaks.oom_killed_flag

  // ---- recommendation
  report.recommendation = recommend({ report, steady, headroom, currentLimitMib, limitStepMib, synthetic, oom, warnings })
  return report
}

function recommend({ report, steady, headroom, currentLimitMib, limitStepMib, synthetic, oom, warnings }) {
  if (synthetic) return { valid: false, reason: 'synthetic data' }
  const nMax = Math.max(0, ...steady.map((x) => x.sessions))
  if (nMax < 1) return { valid: false, reason: 'no level with at least one session' }
  if (oom) return { valid: false, reason: 'the kernel OOM-killed the container during the run (oom_kill > 0): the measured peak is capped by the limit, so it is not the real need. Re-run with a larger WHATSAPP_SIDECAR_MEM_LIMIT before sizing' }
  const useCg = report.hasCgroup
  const val = (o) => (useCg ? o.cg : o.rss)
  const atN = steady.find((x) => x.sessions === nMax)
  const steadyPeak = useCg ? atN.cg_max : atN.rss_max
  const bursts = report.bursts.map((b) => ({ ...b, delta: useCg ? b.delta_cg : b.delta_rss, peak: useCg ? b.peak_cg : b.peak_rss }))
  const burstDelta = Math.max(0, ...bursts.map((b) => b.delta ?? 0))
  const burstAtMax = bursts.length > 0 && bursts.every((b) => b.sessions === nMax)
  const measuredPeak = val({ cg: report.peaks.cg, rss: report.peaks.rss })
  // The peak that is a real observation is `measuredPeak`. If bursts did not happen at the highest session count, add their
  // delta to the highest steady level instead: that composite was never observed as one reading, so it is flagged.
  const composite = steadyPeak + burstDelta
  const basis = Math.max(measuredPeak, burstAtMax || !bursts.length ? 0 : composite)
  const compositeUsed = basis > measuredPeak
  const fit = useCg ? report.fit_cg : report.fit_rss
  const slope = fit ? Math.max(fit.slope, 0) : null
  const limit = roundUp(basis * headroom, limitStepMib)
  const projected = (n) => basis + (slope ?? 0) * (n - nMax)
  const ns = [...new Set([...steady.map((x) => x.sessions).filter((n) => n > 0), nMax + 2, nMax + 4, nMax + 8])].sort((a, b) => a - b)
  const table = ns.map((n) => ({ sessions: n, projected_peak: r1(projected(n)), needs_limit: roundUp(projected(n) * headroom, limitStepMib), kind: n <= nMax ? 'measured range (fit)' : 'EXTRAPOLATED' }))
  // largest N whose projected peak x headroom fits the limit that is in force today
  let fitsToday = null
  if (slope !== null && slope > 0) fitsToday = Math.floor(nMax + (currentLimitMib / headroom - basis) / slope)
  else if (slope === 0) fitsToday = Infinity
  if (!bursts.length) warnings.push('no image-burst / text-load step: the peak does not include bursts, the limit is understated')
  if (bursts.length && !burstAtMax) warnings.push(`bursts were measured at ${bursts.map((b) => b.sessions).join('/')} sessions, below the highest level (${nMax}): their delta is added to the ${nMax}-session level, that combined peak was never observed`)
  return {
    valid: true, basis_metric: useCg ? 'container memory.current (cgroup)' : 'process RSS (no cgroup data)', headroom,
    measured_peak_mib: r1(measuredPeak), steady_peak_at_max_mib: r1(steadyPeak), burst_delta_mib: r1(burstDelta), composite_used: compositeUsed,
    sizing_basis_mib: r1(basis), n_max_measured: nMax,
    mem_limit_mib: limit, max_sessions: nMax,
    rss_refuse_mb: Math.round((limit * 300) / 384),   // keeps today's 300/384 ratio between the refuse guard and the limit
    marginal_mib_per_session: fit ? r1(fit.slope) : null, fit_r2: fit ? Math.round(fit.r2 * 100) / 100 : null, fit_points: fit ? fit.n : 0,
    table, fits_today: fitsToday, current_limit_mib: currentLimitMib,
  }
}

// ---------------------------------------------------------------- text report
const f = (x, d = 1) => (x === null || x === undefined ? 'n/a' : Number.isInteger(x) ? String(x) : x.toFixed(d))
const pad = (s, n) => String(s).padEnd(n)

export function renderReport(r) {
  const out = []
  const line = (s = '') => out.push(s)
  line('WhatsApp sidecar soak report')
  line(`rows ${r.rows} (usable ${r.ok_rows}, failed ${r.error_rows}) over ${f(r.span_h)} h; source: ${r.sources.join(', ') || 'unknown'}`)
  if (r.synthetic) { line(); line('!! SYNTHETIC RUN: fake socket, one Node process, NOT representative of real Baileys sessions !!') }
  if (!r.ok_rows) { for (const w of r.warnings) line(`warning: ${w}`); return out.join('\n') }

  line(); line('1. Baseline (0 sessions)  [MEASURED]')
  line(r.baseline ? `   RSS median ${f(r.baseline.rss_median)} MiB (max ${f(r.baseline.rss_max)})${r.hasCgroup ? `; container memory.current median ${f(r.baseline.cg_median)} MiB` : ''}; ${r.baseline.samples} samples` : '   not measured')

  line(); line('2. Steady levels  [MEASURED]  (medians after dropping the first 25% of each step)')
  line(`   ${pad('step', 12)}${pad('sessions', 10)}${pad('samples', 9)}${pad('RSS med', 10)}${pad('RSS p95', 10)}${pad('RSS max', 10)}${r.hasCgroup ? `${pad('cg med', 10)}${pad('cg max', 10)}anon med` : ''}`)
  for (const s of r.steady) line(`   ${pad(s.step, 12)}${pad(s.sessions, 10)}${pad(s.samples, 9)}${pad(f(s.rss_median), 10)}${pad(f(s.rss_p95), 10)}${pad(f(s.rss_max), 10)}${r.hasCgroup ? `${pad(f(s.cg_median), 10)}${pad(f(s.cg_max), 10)}${f(s.anon_median)}` : ''}`)

  line(); line('3. Marginal cost per session  [FIT through the measured medians]')
  if (r.fit_rss) {
    line(`   RSS: ${f(r.fit_rss.slope)} MiB/session (intercept ${f(r.fit_rss.intercept)} MiB, R2 ${f(r.fit_rss.r2, 2)}, ${r.fit_rss.n} levels)`)
    if (r.fit_cg) line(`   container memory.current: ${f(r.fit_cg.slope)} MiB/session (intercept ${f(r.fit_cg.intercept)} MiB, R2 ${f(r.fit_cg.r2, 2)})`)
    if (r.fit_rss.r2 < 0.9) line('   (R2 below 0.9: cost per session is not linear in this run; do not extrapolate far)')
  } else line('   not computed')

  line(); line('4. Bursts  [MEASURED peak, MiB]')
  if (!r.bursts.length) line('   none recorded')
  for (const b of r.bursts) line(`   ${pad(b.step, 12)}at ${f(b.sessions)} sessions, ${b.samples} samples: peak RSS ${f(b.peak_rss)} (+${f(b.delta_rss)} over '${b.before_step}' ${f(b.before_rss)})${r.hasCgroup ? `; peak container ${f(b.peak_cg)} (+${f(b.delta_cg)}), peak tmpfs/shmem ${f(b.peak_shmem)}` : ''}`)

  line(); line('5. Idle hold drift  [MEASURED]')
  if (!r.hold) line('   not recorded')
  else {
    const h = r.hold
    line(`   ${f(h.hours)} h at ${f(h.sessions)} sessions: RSS ${f(h.start_rss)} -> ${f(h.end_rss)} MiB (${h.drift_mb >= 0 ? '+' : ''}${f(h.drift_mb)}), slope ${f(h.slope_mb_per_h, 2)} MiB/h (R2 ${f(h.r2, 2)})`)
    line(`   1 h mark: ${h.at_1h.reached ? `${f(h.at_1h.rss)} MiB` : 'NOT REACHED'};  24 h mark: ${h.at_24h.reached ? `${f(h.at_24h.rss)} MiB` : 'NOT REACHED'}`)
    if (r.hasCgroup) line(`   container memory.current ${f(h.cg_start)} -> ${f(h.cg_end)} MiB; slab_reclaimable ${f(h.slab_start_mb)} -> ${f(h.slab_end_mb)} MiB`)
    line(`   verdict: ${h.growing ? 'GROWING (possible leak, look at the slope before trusting a limit)' : 'flat within the heuristic (growth < max(10 MiB, 5%) or poor fit)'}`)
  }

  line(); line('6. Reconnect storm (sidecar restart)  [MEASURED]')
  if (!r.storm) line('   not recorded')
  else {
    const s = r.storm
    line(`   waiting for ${f(s.expected)} sessions; ${s.restored ? `restored ${s.restore_s_from_mark} s after the mark${s.restore_s_from_container_start !== null ? `, ${s.restore_s_from_container_start} s after the container started` : ''}` : `NOT restored (last seen ${f(s.last_connected)} connected)`} (sampling resolution ~${f(s.resolution_s)} s${s.weak ? ', WEAK: counts non-logged_out sessions' : ''})`)
    line(`   peak RSS after restart ${f(s.peak_rss)} MiB${r.hasCgroup ? `, peak container ${f(s.peak_cg)} MiB` : ''}; ${s.error_rows} failed samples while it was down`)
  }

  line(); line('7. Safety counters')
  line(`   peak RSS ${f(r.peaks.rss)} MiB; peak container ${f(r.peaks.cg)} MiB; limit in force ${f(r.limit_mib_in_force)} MiB; oom_kill max ${f(r.peaks.oom_kill)}; limit hits ${f(r.peaks.limit_hits)}; State.OOMKilled seen: ${r.peaks.oom_killed_flag}; distinct container starts ${r.peaks.restarts_seen}`)

  line(); line('8. Recommendation')
  const rec = r.recommendation
  if (!rec.valid) line(`   NONE: ${rec.reason}`)
  else {
    line(`   basis: ${rec.basis_metric}; measured peak ${f(rec.measured_peak_mib)} MiB at up to ${rec.n_max_measured} sessions${rec.composite_used ? ` (sizing basis ${f(rec.sizing_basis_mib)} MiB = highest steady level + burst delta, COMPOSITE)` : ''}`)
    line(`   WHATSAPP_SIDECAR_MEM_LIMIT = ${rec.mem_limit_mib}m     [measured peak x ${rec.headroom}, rounded up to 64 MiB]`)
    line(`   WHATSAPP_MAX_SESSIONS      = ${rec.max_sessions}        [the highest session count actually soaked; MEASURED. Going above it is a guess]`)
    line(`   WHATSAPP_RSS_REFUSE_MB     = ${rec.rss_refuse_mb}       [derived: keeps today's 300/384 ratio to the limit]`)
    line()
    line('   Projection (marginal cost from the fit; rows above the highest measured count are EXTRAPOLATED):')
    line(`   ${pad('sessions', 10)}${pad('peak MiB', 11)}${pad('limit needed', 14)}kind`)
    for (const t of rec.table) line(`   ${pad(t.sessions, 10)}${pad(f(t.projected_peak), 11)}${pad(`${t.needs_limit}m`, 14)}${t.kind}`)
    line(`   With today's ${rec.current_limit_mib} MiB limit and x${rec.headroom} headroom: ${rec.fits_today === Infinity ? 'no growth per session seen' : rec.fits_today === null ? 'cannot say (no fit)' : rec.fits_today < 1 ? 'not even one session fits' : `up to ${rec.fits_today} sessions${rec.fits_today > rec.n_max_measured ? ' (EXTRAPOLATED beyond the measured ' + rec.n_max_measured + ')' : ' (inside the measured range)'}`}.`)
  }
  if (r.warnings.length) { line(); line('Warnings'); for (const w of r.warnings) line(`   - ${w}`) }
  return out.join('\n')
}
