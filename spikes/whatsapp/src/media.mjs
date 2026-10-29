// Enforce the media size cap on bytes actually received, not on the sender-declared length.
export class MediaTooLarge extends Error {}

// Protobuf lengths arrive as Long objects, numbers or undefined. Anything unusable fails closed (Infinity).
export const declaredLength = (v) => {
  if (v == null) return Infinity
  const n = typeof v === 'object' && typeof v.toNumber === 'function' ? v.toNumber() : Number(v)
  return Number.isFinite(n) && n >= 0 ? n : Infinity
}

export async function readCapped(stream, max) {
  const chunks = []
  let total = 0
  try {
    for await (const c of stream) {
      total += c.length
      if (total > max) throw new MediaTooLarge(`media exceeds ${max} bytes`)
      chunks.push(c)
    }
  } catch (e) {
    stream.destroy?.()
    throw e
  }
  return Buffer.concat(chunks)
}
