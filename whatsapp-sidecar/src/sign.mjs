// Webhook signing: `X-Luran-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "t.rawbody")>`.
// Same format as spikes/whatsapp/src/sign.mjs. The receiver accepts the current and the previous secret (rotation).
import { createHmac, timingSafeEqual } from 'node:crypto'

export const SIGNATURE_HEADER = 'x-luran-signature'

export function sign(secret, rawBody, ts = Math.floor(Date.now() / 1000)) {
  const mac = createHmac('sha256', secret).update(`${ts}.${rawBody}`).digest('hex')
  return `t=${ts},v1=${mac}`
}

// `secrets` may be one string or a list (current first, then previous). Null/empty entries are skipped.
export function verify(secrets, rawBody, header, now = Math.floor(Date.now() / 1000), skewS = 300) {
  const list = (Array.isArray(secrets) ? secrets : [secrets]).filter(Boolean)
  const m = /^t=(\d+),v1=([0-9a-f]{64})$/.exec(header || '')
  if (!m || !list.length) return false
  const ts = Number(m[1])
  if (!Number.isSafeInteger(ts) || Math.abs(now - ts) > skewS) return false
  const got = Buffer.from(m[2], 'hex')
  let ok = false
  for (const secret of list) {
    const want = Buffer.from(sign(secret, rawBody, ts).split('v1=')[1], 'hex')
    if (timingSafeEqual(want, got)) ok = true   // no early exit: constant work per configured secret
  }
  return ok
}
