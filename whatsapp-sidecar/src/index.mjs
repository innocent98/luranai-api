// Entrypoint: wire config, Postgres, Redis, webhook delivery, session manager and the internal API.
import Redis from 'ioredis'
import { openAuthState } from './auth-state.mjs'
import { createApi } from './api.mjs'
import { loadConfig } from './config.mjs'
import { PgAuthDb } from './db.mjs'
import { RedisLeaseBackend } from './lease.mjs'
import { RedisCounter } from './limiter.mjs'
import { makeLogger } from './log.mjs'
import { SessionManager } from './manager.mjs'
import { currentVersion, makeBaileysSocket, newMessageId } from './wa.mjs'
import { WebhookDelivery } from './webhook.mjs'

const log = makeLogger()
let cfg
try { cfg = loadConfig() } catch (e) { console.error(`config error: ${e.message}`); process.exit(2) }

const db = new PgAuthDb(cfg.databaseUrl)
await db.init()
// Fail fast on commands while Redis is down: the lease must fail closed rather than queue silently.
const redis = new Redis(cfg.redisUrl, { enableOfflineQueue: false, maxRetriesPerRequest: 1, commandTimeout: 3000, lazyConnect: false })
redis.on('error', () => { /* surfaced through lease/counter failures */ })
// Commands fail fast until connected, so give the first connection a moment before restore starts (still fail closed if it never comes).
if (redis.status !== 'ready') await Promise.race([new Promise((r) => redis.once('ready', r)), new Promise((r) => setTimeout(r, 10000))])

const webhook = new WebhookDelivery({ url: cfg.webhookUrl, secret: cfg.webhookSecret, log })
const manager = new SessionManager({
  cfg, log, db,
  emit: (key, event) => webhook.emit(key, event),
  makeSocket: makeBaileysSocket,
  openAuth: openAuthState,
  leaseBackend: new RedisLeaseBackend(redis),
  counter: new RedisCounter(redis),
  newMessageId,
  versionProvider: currentVersion,
  now: () => Date.now(),
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  random: Math.random,
  rssBytes: () => process.memoryUsage().rss,
})

const server = createApi({ manager, token: cfg.token, log })
server.listen(cfg.port, cfg.host, () => log.info({ host: cfg.host, port: cfg.port, maxSessions: cfg.maxSessions }, 'sidecar listening'))
manager.startSweeper()
manager.restoreAll().then((r) => log.info(r, 'restore scheduled')).catch((e) => log.error({ err: e.message }, 'restore failed'))

let closing = false
const shutdown = async (sig) => {
  if (closing) return
  closing = true
  log.info({ sig }, 'shutting down')
  const hard = setTimeout(() => process.exit(1), 15000); hard.unref()
  server.close()
  await manager.shutdown()
  await webhook.drain(5000)
  await db.close().catch(() => {})
  redis.disconnect()
  process.exit(0)
}
process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))
process.on('unhandledRejection', (e) => log.error({ err: e?.message }, 'unhandled rejection'))
process.on('uncaughtException', (e) => { log.error({ err: e?.message }, 'uncaught exception'); process.exit(1) })
