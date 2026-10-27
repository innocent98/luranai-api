// Encrypted-at-rest replacement for Baileys' useMultiFileAuthState.
// Every creds/key file is AES-256-GCM encrypted with SPIKE_AUTH_KEY (32-byte hex).
// Production would use a per-tenant data key (see app/core/encryption.py) in Postgres, not files.
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys'

const seal = (key, plain) => {
  const iv = randomBytes(12)
  const c = createCipheriv('aes-256-gcm', key, iv)
  const ct = Buffer.concat([c.update(plain, 'utf8'), c.final()])
  return Buffer.concat([Buffer.from([1]), iv, c.getAuthTag(), ct])
}
const open = (key, blob) => {
  if (blob[0] !== 1) throw new Error('unknown envelope version')
  const d = createDecipheriv('aes-256-gcm', key, blob.subarray(1, 13))
  d.setAuthTag(blob.subarray(13, 29))
  return Buffer.concat([d.update(blob.subarray(29)), d.final()]).toString('utf8')
}
const safe = (n) => n.replace(/\//g, '__').replace(/:/g, '-')

export async function useEncryptedAuthState(dir, hexKey) {
  const key = Buffer.from(hexKey, 'hex')
  if (key.length !== 32) throw new Error('SPIKE_AUTH_KEY must be 32 bytes hex')
  await fs.mkdir(dir, { recursive: true, mode: 0o700 })
  const file = (n) => path.join(dir, safe(n) + '.enc')
  const write = (n, v) => fs.writeFile(file(n), seal(key, JSON.stringify(v, BufferJSON.replacer)), { mode: 0o600 })
  const read = async (n) => {
    try { return JSON.parse(open(key, await fs.readFile(file(n))), BufferJSON.reviver) } catch { return null }
  }
  const creds = (await read('creds')) || initAuthCreds()
  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const out = {}
          await Promise.all(ids.map(async (id) => {
            let v = await read(`${type}-${id}`)
            if (type === 'app-state-sync-key' && v) v = proto.Message.AppStateSyncKeyData.fromObject(v)
            out[id] = v
          }))
          return out
        },
        set: async (data) => {
          const jobs = []
          for (const type in data) for (const id in data[type]) {
            const v = data[type][id]
            jobs.push(v ? write(`${type}-${id}`, v) : fs.rm(file(`${type}-${id}`), { force: true }))
          }
          await Promise.all(jobs)
        },
      },
    },
    saveCreds: () => write('creds', creds),
    wipe: () => fs.rm(dir, { recursive: true, force: true }),
  }
}
