// Pure message classification. 1:1 text chats only: groups, status, broadcasts, newsletters are ignored; media is never
// downloaded (kind "other", text ""); edits, deletes, reactions and protocol chatter are ignored.
const ONE_TO_ONE = /@(s\.whatsapp\.net|lid)$/
const SAFE_ID = /^[A-Za-z0-9._-]{1,128}$/
const MAX_TEXT = 8192

// Wrapper / chatter content types that must never surface as a message.
const IGNORED = ['protocolMessage', 'editedMessage', 'reactionMessage', 'encReactionMessage', 'senderKeyDistributionMessage',
  'pollUpdateMessage', 'keepInChatMessage', 'messageHistoryBundle', 'requestPhoneNumberMessage']

export const userPart = (jid) => String(jid || '').split('@')[0].split(':')[0]
export const phoneDigitsOf = (jid) => {
  if (!String(jid || '').endsWith('@s.whatsapp.net')) return null
  const u = userPart(jid)
  return /^\d{6,15}$/.test(u) ? u : null
}
export const isOneToOneJid = (jid) => typeof jid === 'string' && jid.length <= 100 && ONE_TO_ONE.test(jid) && !jid.includes('@g.us')

// Unwrap ephemeral/view-once wrappers without importing Baileys (keeps this module pure for tests).
function unwrap(content) {
  let c = content
  for (let i = 0; i < 4 && c; i++) {
    const inner = c.ephemeralMessage?.message || c.viewOnceMessage?.message || c.viewOnceMessageV2?.message
      || c.viewOnceMessageV2Extension?.message || c.documentWithCaptionMessage?.message
    if (!inner) break
    c = inner
  }
  return c
}

/**
 * @returns null (ignore) | { id, fromMe, chatRef, kind, text, pushName, tsS }
 */
export function parseMessage(msg, { selfUsers = [] } = {}) {
  const key = msg?.key
  if (!key?.id || !isOneToOneJid(key.remoteJid) || !SAFE_ID.test(key.id)) return null
  if (selfUsers.includes(userPart(key.remoteJid))) return null            // "message yourself" chat
  const raw = msg.message
  if (!raw || typeof raw !== 'object') return null                        // undecryptable stub / empty
  if (IGNORED.some((k) => raw[k])) return null
  const c = unwrap(raw)
  if (!c || IGNORED.some((k) => c[k])) return null
  const text = typeof c.conversation === 'string' ? c.conversation
    : typeof c.extendedTextMessage?.text === 'string' ? c.extendedTextMessage.text : null
  const hasContent = text !== null || Object.keys(c).some((k) => k !== 'messageContextInfo')
  if (!hasContent) return null
  return {
    id: key.id,
    fromMe: !!key.fromMe,
    chatRef: key.remoteJid,
    kind: text !== null ? 'text' : 'other',
    text: text !== null ? text.slice(0, MAX_TEXT) : '',
    pushName: typeof msg.pushName === 'string' && msg.pushName ? msg.pushName.slice(0, 100) : null,
    tsS: Number(msg.messageTimestamp?.toString?.() ?? msg.messageTimestamp) || 0,
  }
}

// PN digits for an inbound chat: the jid itself, then Baileys' alt field, then the lid mapping. null when unknown.
export async function resolvePhoneDigits(msg, sock) {
  const jid = msg.key.remoteJid
  const direct = phoneDigitsOf(jid) || phoneDigitsOf(msg.key.remoteJidAlt)
  if (direct) return direct
  try {
    const pn = await sock?.signalRepository?.lidMapping?.getPNForLID?.(jid)
    return phoneDigitsOf(pn)
  } catch { return null }
}

// Bounded insertion-ordered set (LRU on insert) for message-id dedupe / "sent by us" tracking.
export class BoundedSet {
  constructor(max) { this.max = max; this.s = new Set() }
  has(v) { return this.s.has(v) }
  add(v) { this.s.delete(v); this.s.add(v); if (this.s.size > this.max) this.s.delete(this.s.values().next().value); return this }
  get size() { return this.s.size }
}
export class BoundedMap {
  constructor(max) { this.max = max; this.m = new Map() }
  get(k) { return this.m.get(k) }
  set(k, v) { this.m.delete(k); this.m.set(k, v); if (this.m.size > this.max) this.m.delete(this.m.keys().next().value); return this }
  get size() { return this.m.size }
}
