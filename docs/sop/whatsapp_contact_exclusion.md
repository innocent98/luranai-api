# SOP: WhatsApp contact exclusion list (T15), 2026-10-29

## What shipped
Branch `feature/whatsapp-contact-exclusion` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges on Adebayo's "merge #N"). Production untouched. This is the gate before the public flip of `WHATSAPP_ENABLED` in production.

A per-connected-number list of contacts Luran ignores entirely: no AI reply, no `conversation_session` (so it never counts against the monthly conversation limit), no stored message, no visitor, no chat row, no dedupe-ledger row. Tenant admins manage it through four endpoints. The dashboard UI is a separate later PR; the contract is `docs/fe-integration-guide-whatsapp-exclusion.md` (captures in `docs/fe-integration-captures/whatsapp-exclusion/`, rig `tests/fakes/live_capture_exclusion.py`). Spec `docs/superpowers/specs/2026-10-29-whatsapp-contact-exclusion-design.md`, plan `docs/superpowers/plans/2026-10-29-whatsapp-contact-exclusion.md`.

## Why
WhatsApp connects through the tenant's own phone number. Without a way to say "never touch this person" the AI answers the tenant's family and friends. Privacy and trust risk, so it blocks the public flip.

## How (decisions and trade-offs)
| Decision | Why / rejected alternative |
|---|---|
| Entries hold `phone_hmac` (peppered, domain-prefixed, `app/channels/identity.py`), a learned `lid_hmac`, an **encrypted** display mask and an optional **encrypted** label. The full number is never stored. | Same privacy posture as `whatsapp_connections`/`whatsapp_chats`. Rejected: storing the number encrypted (no need: matching only needs the HMAC, display only needs the mask). |
| Match on three things: digits from `from_phone_digits`, digits in a phone-JID ref (`<digits>@s.whatsapp.net`), and a learned LID key | The sidecar's `from_ref` is often a LID, and `from_phone_digits` can be null. The owner-typed event carries only `to_ref`. A phone-only list would silently miss LID chats. |
| Chats remember `phone_hmac` (new nullable column on `whatsapp_chats`, an HMAC) once the sidecar resolved the number; adding an entry links the contact's existing LID chat immediately, and an existing chat contributes its key at match time | Opus review: a contact excluded after they already had a LID-keyed chat was not matched until a new event with digits arrived, so agent messages, rating prompts and owner texts could still reach/store them. |
| **Learn the LID**: when a LID-ref event also carries digits that match an entry, store `contact_hmac(ref)` on the entry and delete any session-less chat row for that ref | Makes later events (no digits, owner-typed) match, and removes a pause-only row the owner created before the mapping was known. One LID per number: a new LID replaces the old (a number has one LID). |
| Check in **three places**: webhook before `enqueue_event`; worker `handle_inbound` / `handle_outbound_external` after the kill-switch check and **before** `_record_message_id`; sender `_preflight` | Webhook: the text of an ignored contact never sits in Redis. Worker: authoritative, closes the race where the entry is added after enqueue. Sender: a reply queued before the entry (or an agent typing into an old conversation) cannot reach the contact. |
| The sender drops **every** send to an excluded chat (AI, fallback, agent, rating prompt), not only `guard=True` | The tenant said "never talk to this person through Luran". A silent drop for an agent is the trade-off; the FE guide tells the dashboard to explain it. |
| Adding an entry does **not** delete an existing conversation; it only deletes session-less chat rows (and the visitor they made) | History belongs to the tenant (existing conversation tools delete it). Rejected: auto-purge (destructive, surprising). |
| Tolerant normalisation (`+`, `00`, spaces, dashes, dots, brackets, a literal `(0)`, and the trunk `0` after 234/44/33/49/61/27 is dropped); national format (`0801…`) is `invalid_phone` | A person types numbers like this. Without a country code the number cannot be matched. Connection create stays digits-only. |
| Gating: add and bulk need `can_use_whatsapp` + kill switch (`403 whatsapp_not_enabled`); list and remove are tenant-scoped only | Same as `create_connection`; a tenant can always see and delete its own data. |
| Cap 5,000 entries per connection, bulk 1-200 per request, partial success with per-index codes | Bounded table and request; the FE can show row-level errors. |
| `tenant_id` and `connection_id` both FK `ON DELETE CASCADE`; account purge also deletes explicitly | Same pattern as `whatsapp_chats`; purge does not rely on the cascade (SQLite tests). |

## What's involved
* **Migration** `alembic/versions/wa40_whatsapp_contact_exclusion.py` (`wa40contactexclusion`, down `cl10conversationlimits`, single head). Additive: one new table plus a nullable `whatsapp_chats.phone_hmac` column + index. Downgrade drops it.
* **Model** `WhatsAppContactExclusion` in `app/db/models/whatsapp.py`; unique `(connection_id, phone_hmac)`, index `(connection_id, lid_hmac)`.
* **Matching/cleanup** `app/services/whatsapp_exclusion.py` (`normalize_exclusion_phone`, `is_excluded`, `purge_sessionless_chats`). **CRUD** `app/services/whatsapp_exclusion_service.py`. **Endpoints** `app/api/v1/endpoints/whatsapp.py` (`…/connections/{id}/exclusions`, `/bulk`, `/{exclusion_id}`). **Schemas** `app/schemas/whatsapp.py`.
* **Hooks**: `app/api/v1/endpoints/webhooks.py` (early drop), `app/services/whatsapp_inbound.py` (`dropped_excluded`), `app/services/whatsapp_outbound.py` (`_preflight`), `app/services/account_purge_service.py`.
* **Config**: none new. Uses `WHATSAPP_CONTACT_PEPPER` (already required).

## Verification
* `tests/whatsapp/test_exclusion.py` (SQLite): normalisation matrix, CRUD, per-connection lists, tenant isolation, gating, cap, pagination, bulk, learning, every inbound kind, owner-typed, replay, sender drops (queued reply, LID chat, agent takeover), removal restores handling, webhook never queues, purge.
* `tests/whatsapp/test_exclusion_postgres.py` (real Postgres): savepoint-safe duplicates, per-connection unique, sealed columns, FK cascade, the full drop path with zero rows and LID learning, pause-row cleanup.
* `tests/migrations/test_wa40_contact_exclusion.py` (real Postgres): single head, columns/uniques/FKs match the model, downgrade one step, upgrade again; the whole chain also upgraded to head on an empty scratch DB.
* **Live** against the running app (Postgres, Redis, real API, real arq worker, fake sidecar, LLM stubbed): see the FE guide section 6/9. Webhook-level and worker-level drops both proven; zero rows, zero sends; removal restores the AI.
* **Not verified**: a real phone / real Baileys sidecar; a LID whose number never resolves; the sender-side drop live (unit + Postgres only); production.

## Operate / roll back
* Nothing to configure. Needs `WHATSAPP_CONTACT_PEPPER` (without it every WhatsApp lookup raises, as before). **Do not rotate the pepper** without re-adding the list: every key changes.
* Roll back: revert the PR and run `alembic downgrade cl10conversationlimits` (drops the table). The inbound hooks then no longer exist, so listed contacts would be answered again: do not roll back while WhatsApp is public.
* Worker outcome label `dropped_excluded` in logs (`whatsapp event message.inbound -> dropped_excluded`) is the signal that it is working. No contact data is logged.

## Follow-ups
* **Known gap:** the first message from an excluded contact whose LID has no resolvable digits (sidecar lid mapping empty) when the backend has never seen that contact with a number cannot be recognised.
* **"Nothing stored" is backend-side.** The sidecar still keeps its own hashed chat bookkeeping (reply-window) and holds an inbound image in its short-lived media store (about 10 minutes) before the backend drops the event; a denylist pushed to the sidecar would close it. Mitigation: tell tenants to add contacts straight after connecting (FE copy). Proper fix: "exclude this contact" from an existing conversation (ref-level key), and/or the sidecar always resolving digits (`whatsapp_sidecar.md` open item).
* Optional later: delete-existing-conversation toggle when adding; export; a label edit endpoint; a sidecar-side filter so excluded contacts' events are not even sent (needs the list pushed to the sidecar).
* Dashboard PR in `luran-dashboard` (section 8 of the FE guide).
