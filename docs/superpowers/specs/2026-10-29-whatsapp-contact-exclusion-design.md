# WhatsApp contact exclusion list (T15) — design

Date: 2026-10-29. Status: decided by Adebayo's lead (scope), gaps closed by the builder (marked **[call]**).
Gate for the public flip of `WHATSAPP_ENABLED` in production.

## Why

WhatsApp is plan-driven and connects through the tenant's **own phone number** (Baileys linked device).
Without a way to say "never touch this person", the AI answers the tenant's family and friends on that number.
The tenant needs a per-number list of contacts Luran ignores completely.

## Goals

- Each WhatsApp connection has its own list (not per tenant).
- A listed contact is ignored **entirely**: no AI reply, no `conversation_session`, no stored message, no
  conversation-limit count, no visitor, no `whatsapp_chats` row, no dedupe-ledger row, no owner-pause row.
- The list holds the minimum: a peppered HMAC key per contact, plus an encrypted display mask and an optional
  encrypted label. The full number is **never** stored.
- Tenant admins can add (one / bulk), list and remove. Dashboard UI is a separate later PR.

## Non-goals / anti-goals

- No group exclusion (the sidecar already drops groups, broadcasts, newsletters and the self-chat before the backend).
- No deletion of conversations that already exist for a contact when it is added (history is the tenant's record;
  they can delete it through existing conversation tools). **[call]**
- Not a block on WhatsApp itself: the contact can still message the number; Luran just does not react.
- Not "exclude by name" or by pattern/country.

## How a contact is recognised (the hard part)

The sidecar's `from_ref` is the opaque `remoteJid`, which is often a **LID** (`…@lid`), not a phone. The inbound
event may carry `from_phone_digits` (best effort; null when the LID mapping is empty). The owner-typed event
(`message.outbound_external`) carries **only** `to_ref`. So a list keyed only by phone would miss LID chats.

Entry keys (all peppered HMACs from `app/channels/identity.py`):

| Column | Meaning |
|---|---|
| `phone_hmac` | `phone_hmac(digits)` of the number the tenant typed. Unique per connection. |
| `lid_hmac` | nullable; `contact_hmac(from_ref)` of the LID ref, **learned** the first time an inbound event for this contact carries both a LID `from_ref` and matching `from_phone_digits`. One column suffices: the phone-JID form is derived from digits. |

Match rule for an event on connection C (`app/services/whatsapp_exclusion.py::find_exclusion`):

1. Candidate digits = normalised `from_phone_digits` (inbound only) plus, if the ref is `<digits>@s.whatsapp.net`,
   those digits. Any `phone_hmac` hit on C matches.
2. Otherwise, `contact_hmac(ref) == lid_hmac` on C matches (this is what covers owner-typed events and later
   inbounds whose digits come back null).
3. A chat also remembers `whatsapp_chats.phone_hmac` (an HMAC) once the sidecar resolved the number. An existing chat of the ref contributes that key to the match, and adding an entry links the contact's existing LID chat (`lid_hmac`) at once, so a contact excluded AFTER they were first seen by LID is recognised without waiting for new digits (Opus review finding).
4. On a phone match through a LID ref, **learn** it: set `lid_hmac`, and delete any session-less
   `whatsapp_chats` row for that ref (an owner-pause row created before the mapping was known).

**Known, documented gap [call]:** the very first message from an excluded contact the backend has never seen with a
resolved number (LID, no digits, no chat yet) cannot be recognised and is treated as a normal customer. Mitigations: the
sidecar's lid mapping is normally populated from the owner's contacts; the learn step closes the gap from the
first resolved message on; follow-up: "exclude this contact" from an existing conversation (ref-level key).

## Where it applies (all before any side effect)

1. **Webhook** (`webhooks.py`, before `enqueue_event`): message content of an excluded contact never reaches Redis.
2. **Worker** (`whatsapp_inbound.handle_inbound` / `handle_outbound_external`, right after `_tenant_may_use`, before
   `_record_message_id`): authoritative check (closes the race where the entry is added after enqueue). Outcome
   label `dropped_excluded`.
3. **Sender** (`whatsapp_outbound._preflight`): any queued send (AI reply, fallback, agent message, rating prompt)
   to an excluded chat is dropped (`dropped_excluded`), so a reply queued before the entry was added, or an agent
   typing into an old conversation, cannot reach the contact. **[call]** Block everything, not only guard=True:
   the tenant said "never talk to this person through Luran".
4. Agent takeover edge: inbound is dropped, so an assigned/escalated conversation never receives new customer
   turns and the AI cannot resume on it; point 3 stops the agent side.
5. Adding an entry also deletes existing session-less chat rows of that contact (JID-derived key) on that connection.

## API (tenant admin, `/api/v1/whatsapp`)

Gating: add and bulk add require `can_use_whatsapp` and the kill switch (`403 whatsapp_not_enabled`), like
`create_connection`. List and remove are tenant-scoped only (a tenant can always see and delete its own data).
Another tenant's connection or entry is `404 not_found`. Errors are `{code, message}` via `WhatsAppError`.

| Method + path | Behaviour |
|---|---|
| `POST /connections/{id}/exclusions` `{phone_number, label?}` | 201 entry; 409 `already_excluded`; 422 `invalid_phone`, `own_number`; 409 `exclusion_limit` |
| `POST /connections/{id}/exclusions/bulk` `{entries:[{phone_number,label?}]}` (1..200) | 200 `{added, already_excluded, rejected:[{index,code}]}`; partial success |
| `GET /connections/{id}/exclusions?limit&offset` | `{items,total,limit,offset}`, oldest first |
| `DELETE /connections/{id}/exclusions/{exclusion_id}` | 204 |

Entry out: `{id, phone_masked, label, created_at}`.

Normalisation **[call]** (more tolerant than connection create, which is digits-only): drop a literal `(0)`, strip
spaces, `-`, `.`, `()`; accept a leading `+` or `00`; drop the trunk `0` right after 234/44/33/49/61/27; the rest must be 8–15 digits with no leading 0. A national format (`0801…`) is
`invalid_phone` (no country code to resolve it). Own number (`phone_hmac == connection.phone_hmac`) is `own_number`.
Cap **[call]**: 5,000 entries per connection.

## Data

`whatsapp_contact_exclusions`: id, tenant_id (FK tenants CASCADE), connection_id (FK connections CASCADE),
phone_hmac, lid_hmac?, phone_masked (EncryptedString), label? (EncryptedString, max 60), created_at, updated_at.
Unique `(connection_id, phone_hmac)`; index `(connection_id, lid_hmac)`. Also `whatsapp_chats.phone_hmac` (nullable) + index `(connection_id, phone_hmac)`. Alembic `wa40contactexclusion`,
down_revision `cl10conversationlimits`, additive, reversible (drop table).
Account purge: removed by the connection FK cascade; the purge service also deletes it explicitly (no reliance on
cascade in SQLite tests).

## Verification plan

Unit/API tests (SQLite) for normalisation, CRUD, tenant isolation, gating, every inbound kind, owner-typed, sender
drop, learning; migration round trip and a real-Postgres path (unique constraint, cascade, `IN` match, learning) on
the scratch DB; live capture of every endpoint plus an end-to-end inbound/owner-typed drop against the running app
for the FE guide. Not exercisable live here: a real Baileys sidecar / phone (events are injected through the signed
webhook exactly as the sidecar sends them).
