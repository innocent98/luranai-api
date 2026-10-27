import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readdir, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { randomBytes } from 'node:crypto'
import { useEncryptedAuthState } from '../src/crypto-store.mjs'

test('creds round-trip, ciphertext on disk, wrong key cannot read', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'wa-store-'))
  const k1 = randomBytes(32).toString('hex')
  const a = await useEncryptedAuthState(dir, k1)
  const noiseBefore = a.state.creds.noiseKey.public.toString('hex')
  await a.saveCreds()
  await a.state.keys.set({ 'pre-key': { 1: { private: Buffer.from('p'), public: Buffer.from('q') } } })
  const files = await readdir(dir)
  assert.ok(files.includes('creds.enc') && files.includes('pre-key-1.enc'))
  const raw = await readFile(path.join(dir, 'creds.enc'))
  assert.equal(raw.includes(Buffer.from(noiseBefore, 'hex')), false)
  const b = await useEncryptedAuthState(dir, k1)
  assert.equal(b.state.creds.noiseKey.public.toString('hex'), noiseBefore)
  assert.equal((await b.state.keys.get('pre-key', ['1']))['1'].public.toString(), 'q')
  const c = await useEncryptedAuthState(dir, randomBytes(32).toString('hex'))
  assert.notEqual(c.state.creds.noiseKey.public.toString('hex'), noiseBefore)
})
