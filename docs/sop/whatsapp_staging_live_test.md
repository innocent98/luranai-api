# SOP: WhatsApp live test on staging, 2026-10-28

## What shipped
The WhatsApp quick-connect channel running on **staging** end to end with a real number: sidecar deployed through CD, a tenant paired from the dashboard, AI answering from the tenant's KB, an escalation that rang the Flutter agent app, agent takeover and replies in both directions, and resolve. Production is untouched.

## Why
Earlier proofs were local (one Mac) or fakes. This one used the real staging stack on the shared VPS, the real push path to a phone and the real agent app build against the staging API.

## How
- Sidecar deployed by dispatching `cd.yml` from a branch (GitHub runs `workflow_run`-triggered workflows from the default branch `main`, so pipeline changes on `develop` do not affect automatic deploys until promoted). First real run exposed a bug (scp container could not read a 600 `.env`); fixed in luranai-api #165.
- Tenant granted `can_use_whatsapp` and one number from the platform-admin capabilities editor (the editor had no WhatsApp control; added in luran-dashboard #94, which also stops it sending read-only fields).
- Number paired from the tenant dashboard by its owner; a second personal WhatsApp account messaged it.

## Verification (observed)
| Check | Result |
|---|---|
| Sidecar deploy | healthy; smoke PASS: signed webhook acknowledged (200), bad signature refused (401), Redis, dedicated Postgres role works, role can SELECT 0 public tables |
| Pairing | `awaiting_pair` then 515 restart then `connected`; dashboard shows "Connected", 1 of 1 number |
| Sidecar memory | 70 MiB of 384 MiB with one live session (single session, short run: not a capacity figure) |
| AI answers from the KB | yes, several turns |
| Escalation | the customer's request for a person rang the Flutter agent app (staging build) |
| Agent takeover | agent joined (system notice stored), replies landed on the customer's WhatsApp, customer replies appeared in the app |
| Resolve | worked; a rating request was sent to the customer as plain text |
| Stored conversation | 21 messages: 9 customer, 6 AI, 5 agent, 1 system, plus the rating request; AI replies stored correctly |
| Images | not supported (known): stored as a placeholder; the "please type" fallback was also sent while the agent owned the conversation |

## Findings
1. The agent app's live takeover screen does not show earlier AI turns. By design (its model comment: the WebSocket replay carries only customer and human-agent turns); the History and Missed screens use the REST transcript, which has every role.
2. The rating request goes out as text but nothing parses a reply (digit handling is plan task T6).
3. Images: placeholder only; fallback text also fires during a human takeover.
4. Staging API memory floor: automatic deploys use `main`'s workflow, so the 768m floor was not applied to the latest automatic deploy (limit back to 500m, using 205 MiB).

## Operate / roll back
Kill switch `WHATSAPP_ENABLED=false` (staging env), or `docker stop luran_staging_whatsapp_sidecar`. Disconnect a number from the tenant dashboard. Prod has none of this.

## Follow-ups
See the four new items at the end of "Deferred follow-ups (WhatsApp)" in the master checklist.
