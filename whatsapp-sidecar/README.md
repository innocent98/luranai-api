# whatsapp-sidecar

Multi-session WhatsApp **linked-device** service (Baileys `7.0.0-rc14`, exact pin) promoted from `spikes/whatsapp/`.
A tenant's number is linked with an 8-character pairing code; inbound messages go to the Luran backend as signed
webhooks, replies are sent back through this service. Internal only: no published port, bearer-token API.

Record of what shipped, operations and open risks: [`docs/sop/whatsapp_sidecar.md`](../docs/sop/whatsapp_sidecar.md).
Contract with the backend: `docs/sop/whatsapp_sidecar.md` section "Contract as built".

```bash
cd whatsapp-sidecar
npm ci --ignore-scripts
npm test                      # fake-socket suite; integration tests skip unless TEST_DATABASE_URL + TEST_REDIS_URL are set
npm audit signatures
```

| Path | Purpose |
|---|---|
| `src/index.mjs` | wiring, SIGTERM handling |
| `src/session.mjs` | one session: lease, auth, socket, status mapping, reply-only sends, owner detection |
| `src/manager.mjs` | capacity guard, create/delete, staggered boot restore, shutdown |
| `src/api.mjs` | internal HTTP API (bearer, constant-time compare) |
| `src/auth-state.mjs`, `src/db.mjs`, `src/crypto.mjs` | AES-256-GCM encrypted, batched Postgres auth store (`whatsapp_sidecar` schema) |
| `src/lease.mjs` | Redis single-writer lease per session, fail closed |
| `src/webhook.mjs`, `src/sign.mjs` | signed, retried, ordered webhook delivery |
| `src/media.mjs`, `src/image-fetch.mjs` | image bytes: capped reads, bounded in-memory store, concurrency gate; SSRF-guarded outbound image fetch |
| `src/wa.mjs` | the only runtime Baileys socket construction |
| `scripts/soak.mjs`, `soak-analyze.mjs`, `soak-lib.mjs`, `soak-make-images.mjs`, `soak-synthetic.mjs` | N-session RAM soak: collector (CSV of `/health` + container cgroup memory + step marker), analysis with recommended limits, test-image generator, fake-socket dry run. **Tooling built and unit tested; the real soak has not been run.** Runbook: `docs/sop/whatsapp_sidecar_soak.md` |

Variable names: `.env.example`. Never commit values.
