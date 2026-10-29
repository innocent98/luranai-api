// Logging with PII hygiene: never log message text, phone numbers or pairing codes. Only masked identifiers.
import pino from 'pino'

// Error messages from Baileys/pg/undici can embed identifiers: log class and code only, never the message.
export const errName = (e) => `${e?.constructor?.name || 'Error'}${e?.code ? `:${e.code}` : ''}`

export const mask = (v) => {
  const s = String(v ?? '')
  if (!s) return s
  const user = s.split('@')[0].split(':')[0]
  return user.length <= 6 ? '***' : `${user.slice(0, 3)}***${user.slice(-2)}`
}

export const makeLogger = (level = process.env.LOG_LEVEL || 'info') => pino({
  level,
  // Defence in depth: if a caller ever logs these keys by mistake, they are removed.
  redact: { paths: ['*.text', '*.pairing_code', '*.pairingCode', '*.code_value', '*.phone', '*.phone_digits', '*.to_ref', '*.from_ref', '*.token', '*.secret', 'text', 'pairing_code', 'pairingCode', 'phone_digits'], censor: '[redacted]' },
})

// Baileys' own logger stays silent: its debug output includes message contents and identifiers.
export const silentLogger = () => pino({ level: 'silent' })
