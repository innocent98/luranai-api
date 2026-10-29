// The only module (besides auth-state.mjs) that touches Baileys at runtime. Everything else receives a socket factory,
// so the session logic is tested against a fake socket with no network.
import makeWASocket, {
  Browsers, downloadMediaMessage, fetchLatestBaileysVersion, generateMessageIDV2, isJidBroadcast, isJidGroup, isJidNewsletter, makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys'
import { silentLogger } from './log.mjs'

const logger = silentLogger()   // Baileys debug output contains identifiers and message contents: keep it silent

export function makeBaileysSocket({ auth, version, getMessage }) {
  return makeWASocket({
    ...(version ? { version } : {}),
    logger,
    auth: { creds: auth.state.creds, keys: makeCacheableSignalKeyStore(auth.state.keys, logger) },
    browser: Browsers.macOS('Chrome'),          // valid [platform, browser, version] tuple; custom names break pairing
    printQRInTerminal: false,
    syncFullHistory: false,
    shouldSyncHistoryMessage: () => false,
    markOnlineOnConnect: false,
    generateHighQualityLinkPreview: false,
    emitOwnEvents: false,                       // our own sends are not echoed back as upserts
    shouldIgnoreJid: (jid) => isJidBroadcast(jid) || isJidGroup(jid) || isJidNewsletter(jid),
    getMessage,
  })
}

// Decrypting download as a stream: the caller caps what it reads (MediaStore never sees more than the cap) and destroys it early.
// `msg` is the sanitized {key, message:{imageMessage:{url:'', directPath, mediaKey, mimetype}}} built by the session, and the
// host is pinned, so a crafted imageMessage can never make us request an arbitrary URL (the sender controls `url`).
export const MEDIA_HOST = 'mmg.whatsapp.net'
export const downloadMedia = (msg, sock) => downloadMediaMessage(msg, 'stream', { host: MEDIA_HOST }, { logger, reuploadRequest: sock.updateMediaMessage })

export const newMessageId = (userId) => generateMessageIDV2(userId)

// One shared lookup for all sessions, cached 6 h. Falls back to Baileys' bundled version if the lookup fails.
let cached = null
export async function currentVersion() {
  if (cached && Date.now() - cached.at < 6 * 3600 * 1000) return cached.version
  try {
    const r = await fetchLatestBaileysVersion({ signal: AbortSignal.timeout(5000) })
    if (r?.version) { cached = { version: r.version, at: Date.now() }; return r.version }
  } catch { /* fall through */ }
  return cached?.version
}
