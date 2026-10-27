// Environment parsing. Values are read once at boot; secrets are never logged or echoed (errors name the variable only).
const int = (env, k, d, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) => {
  const raw = env[k]
  if (raw === undefined || raw === '') return d
  const n = Number(raw)
  if (!Number.isInteger(n) || n < min || n > max) throw new Error(`invalid env ${k}: expected integer in [${min}, ${max}]`)
  return n
}

export function loadConfig(env = process.env) {
  const missing = []
  const need = (k) => { if (!env[k]) missing.push(k); return env[k] }
  const cfg = {
    token: need('WHATSAPP_SIDECAR_TOKEN'),
    webhookSecret: need('WHATSAPP_WEBHOOK_SECRET'),
    webhookSecretPrevious: env.WHATSAPP_WEBHOOK_SECRET_PREVIOUS || null,
    masterKeyHex: need('WHATSAPP_AUTH_MASTER_KEY'),
    backendBase: need('BACKEND_BASE'),
    databaseUrl: need('WHATSAPP_SIDECAR_DATABASE_URL'),
    redisUrl: need('WHATSAPP_SIDECAR_REDIS_URL'),
    host: env.LISTEN_HOST || '127.0.0.1',
    port: int(env, 'PORT', 8787, { min: 1, max: 65535 }),
    maxSessions: int(env, 'MAX_SESSIONS', 3, { min: 1, max: 50 }),
    rssRefuseMb: int(env, 'RSS_REFUSE_MB', 400, { min: 64 }),
    sendPerMin: int(env, 'SEND_PER_MIN', 6, { min: 1, max: 60 }),
    dailySendCap: int(env, 'DAILY_SEND_CAP', 200, { min: 1 }),
    replyWindowHours: int(env, 'REPLY_WINDOW_HOURS', 24, { min: 1 }),
    restoreStaggerMs: int(env, 'RESTORE_STAGGER_MS', 2000, { min: 0 }),
    leaseTtlMs: int(env, 'LEASE_TTL_MS', 30000, { min: 3000 }),
    pairingTtlMs: int(env, 'PAIRING_TTL_MS', 600000, { min: 60000 }),
  }
  if (missing.length) throw new Error(`missing required env: ${missing.join(', ')}`)
  if (cfg.token.length < 32) throw new Error('WHATSAPP_SIDECAR_TOKEN must be at least 32 characters')
  if (cfg.webhookSecret.length < 32) throw new Error('WHATSAPP_WEBHOOK_SECRET must be at least 32 characters')
  if (!/^[0-9a-fA-F]{64,}$/.test(cfg.masterKeyHex) || cfg.masterKeyHex.length % 2) {
    throw new Error('WHATSAPP_AUTH_MASTER_KEY must be hex, at least 32 bytes (64 hex chars)')
  }
  try { new URL(cfg.backendBase) } catch { throw new Error('BACKEND_BASE must be an absolute URL') }
  cfg.backendBase = cfg.backendBase.replace(/\/+$/, '')
  cfg.webhookUrl = `${cfg.backendBase}/api/v1/webhooks/whatsapp/linked`
  return cfg
}
