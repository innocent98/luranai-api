# WhatsApp pairing-code spike (isolated, NOT part of the app)

Thin Node sidecar on `@whiskeysockets/baileys` **7.0.0-rc14 (exact pin, `--ignore-scripts`)** that proves the
"enter number -> 8-char pairing code -> linked -> inbound webhook -> reply" flow. Results and evidence:
`docs/sop/whatsapp_pairing_spike.md`. Production design: `docs/superpowers/plans/2026-10-27-whatsapp-channel-backend.md`.

Never wired into `app/`, `docker-compose*.yml` or CI. Do not deploy.

| File | Purpose |
|---|---|
| `src/server.mjs` | Sidecar: `/pair`, `/status`, `/send`, `/media/:id`, `/allow-last`. Binds 127.0.0.1, bearer-token auth |
| `src/crypto-store.mjs` | AES-256-GCM encrypted-at-rest replacement for Baileys' file auth store |
| `src/sign.mjs` | Webhook signing: HMAC-SHA256 over `{ts}.{body}`, 5 min replay window |
| `src/receiver.mjs` | Stand-in for the Luran backend (verify, dedupe, log metadata, auto-reply) |
| `test/*.test.mjs` | Unit tests (`npm test`): signing + encrypted store |
| `captures/events.sanitized.jsonl` | Real webhook events from the run (sender id redacted) |

## Safety rails baked in
- Env-only secrets (`SPIKE_AUTH_KEY`, `SPIKE_WEBHOOK_SECRET`, `SPIKE_API_TOKEN`, `SPIKE_TEST_NUMBER`); none are in the repo.
- `/pair` refuses any number except `SPIKE_TEST_NUMBER`.
- `/send` is reply-only (the chat must have messaged us) AND needs an explicit operator consent (`/allow-last`).
- Per-minute send cap and a human-like composing delay.
- Auth state lives outside the repo (`AUTH_DIR`); `.gitignore` blocks `.auth/`, `.media/`, `*.env`.

## Re-run (needs a dedicated test number and a person holding that phone)
```bash
cd spikes/whatsapp && npm ci --ignore-scripts
set -a; . /path/outside/repo/spike.env; set +a   # the four env vars above
node src/receiver.mjs & node src/server.mjs &
curl -X POST localhost:8787/pair -H "authorization: Bearer $SPIKE_API_TOKEN" -d "{\"number\":\"$SPIKE_TEST_NUMBER\"}"
```
