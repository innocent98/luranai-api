// AES-256-GCM envelope with a per-session key = HKDF-SHA256(master, info="luran-wa-auth:v1:<session_key>").
// The GCM additional-authenticated-data binds each ciphertext to (session_key, record kind, record id), so a row
// copied to another session or another key slot fails authentication instead of decrypting.
import { createCipheriv, createDecipheriv, createHmac, hkdfSync, randomBytes } from 'node:crypto'

const VERSION = 1

export function deriveSessionKey(masterHex, sessionKey) {
  const master = Buffer.from(masterHex, 'hex')
  if (master.length < 32) throw new Error('master key must be at least 32 bytes')
  return Buffer.from(hkdfSync('sha256', master, Buffer.alloc(0), `luran-wa-auth:v1:${sessionKey}`, 32))
}

const aad = (sessionKey, kind, id) => Buffer.from(`${sessionKey}\0${kind}\0${id}`, 'utf8')

export function seal(key, plaintext, sessionKey, kind, id) {
  const iv = randomBytes(12)
  const c = createCipheriv('aes-256-gcm', key, iv)
  c.setAAD(aad(sessionKey, kind, id))
  const ct = Buffer.concat([c.update(plaintext, 'utf8'), c.final()])
  return Buffer.concat([Buffer.from([VERSION]), iv, c.getAuthTag(), ct])
}

export function open(key, blob, sessionKey, kind, id) {
  if (!blob || blob.length < 30 || blob[0] !== VERSION) throw new Error('bad envelope')
  const d = createDecipheriv('aes-256-gcm', key, blob.subarray(1, 13))
  d.setAAD(aad(sessionKey, kind, id))
  d.setAuthTag(blob.subarray(13, 29))
  return Buffer.concat([d.update(blob.subarray(29)), d.final()]).toString('utf8')
}

// Deterministic lookup key for chat refs, so the database never holds a raw JID/phone (reply-only table).
export const chatHash = (key, chatRef) => createHmac('sha256', key).update(`chat\0${chatRef}`).digest('hex')
