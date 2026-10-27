// Webhook signing: HMAC-SHA256 over `${timestamp}.${rawBody}`; header `X-Luran-Signature: t=<unix>,v1=<hex>`.
// Receiver rejects bad MACs and timestamps outside a 5 minute window (replay guard).
import { createHmac, timingSafeEqual } from 'node:crypto'

export function sign(secret, rawBody, ts = Math.floor(Date.now() / 1000)) {
  const mac = createHmac('sha256', secret).update(`${ts}.${rawBody}`).digest('hex')
  return `t=${ts},v1=${mac}`
}
export function verify(secret, rawBody, header, now = Math.floor(Date.now() / 1000), skew = 300) {
  const m = /^t=(\d+),v1=([0-9a-f]{64})$/.exec(header || '')
  if (!m) return false
  const ts = Number(m[1])
  if (Math.abs(now - ts) > skew) return false
  const want = Buffer.from(sign(secret, rawBody, ts).split('v1=')[1], 'hex')
  return timingSafeEqual(want, Buffer.from(m[2], 'hex'))
}
