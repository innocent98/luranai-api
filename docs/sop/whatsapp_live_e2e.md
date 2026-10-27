# SOP: WhatsApp live end-to-end (local), 2026-10-27

## What shipped
The first live end-to-end run of the WhatsApp quick-connect slice (backend #153, sidecar #152, dashboard #88) against a real test number and a second personal WhatsApp account, plus one bug fix found on the way: worker startup crash (#154, `6f93638`). Evidence only; no new feature code beyond #154.

## Why
Every earlier proof used fakes (fake sidecar, fake socket, mock dashboard) or the single-session spike. This run joined the real pieces: dashboard -> backend -> real sidecar -> WhatsApp -> backend pipeline -> real LLM and real KB.

## How
Local stack on one Mac, merged code at `develop` `0052f96` (+ worker fix), dedicated throwaway Postgres, Redis and Qdrant containers, sidecar `MAX_SESSIONS=1`. All third-party credentials (SMTP, Paystack, Firebase, Cloudinary, ElevenLabs) neutralised in the local env; only the OpenAI key was live. Dashboard drove the pairing (number -> code -> connected). Test number paired by its owner; a second personal WhatsApp sent the messages. Local Python was 3.14 (arq needs an explicit event loop there; production image is 3.11, so this is local-only).

## Verification (all observed, 2026-10-27)
| Step | Result |
|---|---|
| Pairing code via dashboard | Code shown, linked on the phone, status `awaiting_pair` -> `connecting` -> `connected` |
| Inbound text | Reached the signed webhook, stored; session `source='whatsapp'` persisted |
| AI reply | Delivered to the personal WhatsApp. First two answers ignored the KB (see findings); after fixing the KB the answer came from it ("Monday to Saturday at 8am, closed Sundays and public holidays"), generated in about 2 s plus the sidecar send delay |
| Photo | `other_kind_reply`: fixed 'please type' text arrived on the phone |
| Owner types on the phone | `message.outbound_external` -> stored as HUMAN_AGENT `meta.source=owner_phone`; `human_until` set 120 min ahead |
| Customer messages while paused | Two messages logged `stored_owner_paused`; the AI did not reply (confirmed on the phone) |
| Owner/customer id match | Owner message landed in the same chat as the customer's (one chat, live) |
| Unlink from phone | `logged_out` (401) within 3 s, connection `logged_out_from_phone`, `auth_blobs` rows wiped (0), dashboard showed the 'WhatsApp was unlinked' banner |
| Sidecar RSS | 128.6 MB idle with 0 sessions; 101.9 MB then 45.1 MB with one live session (single session, short run: not a soak or capacity figure) |
Not proven: more than one session, long-run stability, images, staging, any non-Nigerian number, the LID vs phone identity question beyond one chat.

## Findings
1. **Worker crash (fixed, #154):** `worker.py` printed `f.__name__` for every registered job; the WhatsApp jobs use arq's `func(...)` wrapper (no `__name__`), so the worker exited at boot. A staging deploy of `0052f96` shipped this briefly (staging worker); the fixed deploy followed. Prod was never affected.
2. **First responder:** a tenant created before it has a KB defaults to human-first, so the AI stays silent on WhatsApp with no explanation. The per-connection override in the plan is not built.
3. **KB gotchas hit during setup:** the text-upload endpoint stores rows but no vectors; short documents fail in the chunker silently; one long multi-topic chunk scored under the 0.35 retrieval cutoff; the trial plan allows 2 sources.
4. Smaller: the fallback reply is not stored in the transcript; no team alert during owner pause; conversation list shows the WhatsApp sender name as 'Anonymous visitor' with no channel badge.

## Operate / roll back
Nothing to operate: local stack, torn down (processes stopped, containers removed, env copy and generated secrets deleted). Staging runs the merged backend; the sidecar is NOT deployed there. Prod has none of this (`/whatsapp/consent` is 404 on prod).

## Follow-ups
See "Deferred follow-ups (WhatsApp)" in the master checklist (items added 2026-10-27).
