// Stand-in for the Luran backend: verifies HMAC, dedupes on event id, logs metadata, auto-replies via the sidecar.
import http from 'node:http'
import { promises as fs } from 'node:fs'
import { verify } from './sign.mjs'

const SECRET = process.env.SPIKE_WEBHOOK_SECRET, TOKEN = process.env.SPIKE_API_TOKEN
const SIDECAR = process.env.SIDECAR_URL || 'http://127.0.0.1:8787'
if (!SECRET || !TOKEN) { console.error('missing env'); process.exit(2) }
const seen = new Set()
await fs.mkdir('captures', { recursive: true })
const rec = (o) => fs.appendFile('captures/events.jsonl', JSON.stringify(o) + '\n')

http.createServer((req, res) => {
  let raw = ''
  req.on('data', (c) => (raw += c))
  req.on('end', async () => {
    if (req.method !== 'POST' || req.url !== '/hook') { res.writeHead(404); return res.end() }
    if (!verify(SECRET, raw, req.headers['x-luran-signature'])) { await rec({ at: new Date().toISOString(), rejected: 'bad_signature' }); res.writeHead(401); return res.end() }
    const ev = JSON.parse(raw)
    if (seen.has(ev.id)) { res.writeHead(200); return res.end('dup') }
    seen.add(ev.id)
    res.writeHead(200); res.end('ok')
    // metadata only: never store the sender JID, only the masked form
    const { from, mediaUrl, ...safe } = ev
    await rec({ ...safe, hmac: 'verified' })
    if (ev.type === 'message.inbound' && ev.kind !== 'other') {
      const reply = ev.kind === 'image' ? `Got your image (${ev.bytes} bytes, sha256 ${ev.sha256.slice(0, 8)}). Luran spike receiver here.` : `Echo from Luran spike: ${ev.text}`
      try {
        const r = await fetch(`${SIDECAR}/send`, { method: 'POST', headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' }, body: JSON.stringify({ to: from.split('@')[0], text: reply }) })
        await rec({ at: new Date().toISOString(), reply: r.status })
      } catch (e) { await rec({ at: new Date().toISOString(), replyError: e.message }) }
    }
  })
}).listen(8788, '127.0.0.1', () => console.log('receiver on 8788'))
