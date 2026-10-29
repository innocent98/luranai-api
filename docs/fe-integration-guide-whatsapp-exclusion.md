# FE integration guide: WhatsApp contact exclusion list (T15), 2026-10-29

Audience: the dashboard (tenant app). Backend branch `feature/whatsapp-contact-exclusion`. The dashboard UI is a
separate later PR; section 8 lists exactly what it needs.

Every body below was copied from a real response of a locally running app (migrated Postgres 15, Redis 7, the fake
sidecar, arq worker, LLM stubbed), captured by `tests/fakes/live_capture_exclusion.py` into
`docs/fe-integration-captures/whatsapp-exclusion/` (file numbers in brackets). Placeholders: connection and entry ids
are throwaway local UUIDs; phone numbers are fake (`234800000xxxx`). Auth is the normal tenant-admin bearer token; the
captures replace it with `<admin-jwt-placeholder>`.

## 1. What the feature is, in the FE's terms
A tenant connects its **own** WhatsApp number, so its family and friends can message that number too. Without this
list the AI would answer them. The **exclusion list** is a list of phone numbers, **per connected number**, that Luran
ignores completely: no reply, no conversation (so it does not count toward the monthly conversation limit), no stored
message, nothing in the inbox. Luran does not remember anything about the contact beyond the list entry (a masked
number and an optional label the tenant typed).

It is a Luran-side rule only. The person can still message the tenant on WhatsApp; the tenant just sees it on the phone.

## 2. Endpoints
Base: `/api/v1/whatsapp/connections/{connection_id}/exclusions`. All need a tenant admin. `connection_id` is the id from
`GET /api/v1/whatsapp/connections`. Another tenant's connection id, or an unknown one, is `404 not_found` (never 403).

| Method + path | Purpose | Success |
|---|---|---|
| `GET …/exclusions?limit=&offset=` | list, oldest first | 200 page |
| `POST …/exclusions` | add one | 201 entry |
| `POST …/exclusions/bulk` | add up to 200 at once | 200 summary |
| `DELETE …/exclusions/{exclusion_id}` | remove one | 204, empty body |

There is no edit. To change a label or a number, remove and add again.

### 2.1 Add one [05]
Request `POST …/exclusions`
```json
{"phone_number": "+234 800 000 0100", "label": "Mum"}
```
Response `201`
```json
{"id": "0ec7a56e-4635-4023-96cf-41c6c4dfdb04", "phone_masked": "+234••••••0100", "label": "Mum", "created_at": "2026-10-29T22:50:52.630732Z"}
```
* `phone_number` is what the user typed: spaces, dashes, dots, brackets, a leading `+` or `00`, a literal `(0)` and the
  trunk `0` right after 234/44/33/49/61/27 (`+234 0803…`) are accepted and normalised by the server. It must include the **country code**. `label` is optional (max 60 characters kept, blank
  becomes `null`).
* The full number is never returned (not even here). `phone_masked` is `+` + the first 3 digits + dots + the last 4 digits.
* Without a label [07, 08]: `"label": null`.

### 2.2 List [01, 08, 09]
`GET …/exclusions` with no entries [01]:
```json
{"items": [], "total": 0, "limit": 100, "offset": 0}
```
With two entries [08] the `items` are the same objects as 2.1, `"total": 2`. Paging [09]: `?limit=1&offset=1` returns
`{"items": [<second entry>], "total": 2, "limit": 1, "offset": 1}`. `limit` is 1 to 500 (default 100), `offset` >= 0.
`total` is the whole list, not the page.

### 2.3 Bulk add [11]
Request `POST …/exclusions/bulk`
```json
{"entries": [
  {"phone_number": "2348000000102", "label": "Dad"},
  {"phone_number": "2348000000100"},
  {"phone_number": "2348000000102"},
  {"phone_number": "08000000103"},
  {"phone_number": "+2348000000001"},
  {"phone_number": "+234 800 000 0104"}
]}
```
Response `200`
```json
{"added": 2, "already_excluded": 2, "rejected": [{"index": 3, "code": "invalid_phone"}, {"index": 4, "code": "own_number"}]}
```
It is **partial success**: valid new numbers are added, the rest are reported. `already_excluded` counts numbers
already on the list **and** repeats inside the same request (here index 1 was already listed and index 2 repeats
index 0). `rejected[].index` is the 0-based position in the request, `code` is `invalid_phone` or `own_number`;
no other text is returned (the FE already has the row). 1 to 200 entries per request: chunk a bigger paste.

### 2.4 Remove [21, 22]
`DELETE …/exclusions/{exclusion_id}` returns `204` with no body. Removing the same id again is `404`
`{"code": "not_found", "message": "Excluded contact not found"}`. After removal Luran treats that number as an ordinary
customer again from the next message.

## 3. Error shapes the FE must handle
Two shapes exist. **Trap:** most errors are `{code, message}`, but request-validation errors for query/body *types*
are FastAPI's default `{"detail": [...]}`.

| Case | Status | Body (verbatim) | Capture |
|---|---|---|---|
| Number has no country code (`08000000100`) | 422 | `{"code": "invalid_phone", "message": "Enter the number with its country code, e.g. +234 801 234 5678. Numbers starting with 0 and no country code cannot be matched."}` | [02] |
| Not a number (`mum`) | 422 | same `invalid_phone` body | [03] |
| The connected number itself | 422 | `{"code": "own_number", "message": "That is the number connected to Luran; it cannot be excluded."}` | [04] |
| Already on the list (single add) | 409 | `{"code": "already_excluded", "message": "This number is already on the exclusion list."}` | [06] |
| Bulk with 0 entries (or > 200) | 422 | `{"code": "invalid_bulk", "message": "Send between 1 and 200 numbers per request."}` | [12] (0 entries) |
| `limit=0` on the list | 422 | `{"detail": [{"type": "greater_than_equal", "loc": ["query", "limit"], "msg": "Input should be greater than or equal to 1", "input": "0", "ctx": {"ge": 1}}]}` | [10] |
| Unknown / other tenant's connection | 404 | `{"code": "not_found", "message": "WhatsApp connection not found"}` | [14, 17] |
| Other tenant's connection, add or delete | 404 | same `not_found` | [15, 16] |
| Add or bulk when WhatsApp is not enabled for the tenant | 403 | `{"code": "whatsapp_not_enabled", "message": "WhatsApp is not enabled for this account."}` | [19] |
| No/invalid token | 401 | `{"detail": "Could not validate credentials"}` | captured with curl, not in the files |

Not exercised live (shapes come from the code and the unit tests, not a capture): `409 exclusion_limit`
(`{"code": "exclusion_limit", "message": "A number can have at most 5000 excluded contacts."}`, both for add and for a
bulk that would pass the cap; a refused bulk adds nothing), `503 whatsapp_misconfigured`, and a bulk body with more
than 500 entries (FastAPI `{"detail": [...]}` shape, because the schema cap is 500 and the service cap is 200).

## 4. Gating (when to show the screen)
* **Add and bulk add** need WhatsApp enabled for the tenant (plan/override **and** the platform switch): otherwise `403
  whatsapp_not_enabled` [19]. Gate the whole WhatsApp section on `features.whatsapp.platform_available && enabled`
  exactly like the rest of the WhatsApp UI (see `fe-integration-guide-whatsapp-default-on.md`).
* **List and remove are not plan-gated** [20]: a tenant can always see and delete its own list, even if WhatsApp was
  revoked.

## 5. Field-nesting traps
* The list is **per connection**. The same person on a tenant's second number must be added again on that number.
* `phone_masked` and `label` are the only contact data. There is no phone, no id of the contact, no "chat".
* The list response is a page object (`items/total/limit/offset`), not a bare array like `GET /whatsapp/connections`.
* `204` has no body: do not call `.json()` on it.

## 6. What happens to a listed contact (all verified live)
`pipeline-evidence.json`: database row counts (`whatsapp_chats`, `conversation_sessions`, `messages`, `visitors`,
`whatsapp_inbound_messages`) and the number of sends the sidecar received were all `0` before. Then five events from
the excluded contact were delivered through the real signed webhook: a text from the phone-form ref, a text from a LID
ref with the number resolved, a text from the same LID with **no** number, a voice note, and the **owner typing** to
that contact. Every webhook answered `{"status": "ignored"}`, and every count stayed `0`, nothing was sent.
A real customer on the same number was answered (`after_customer`: 1 chat, 1 conversation, 2 messages, 1 send).
After the entry was removed the same number was answered normally (`after_removal`).

`worker-race-evidence.json` (produced by a short ad-hoc script, steps in its `note`; not in the repo): an event queued **before** the number was added (the worker was stopped) was dropped by
the worker itself once it ran (`dropped_excluded`), counts unchanged, nothing sent.

Not exercised live: a **real** WhatsApp number and Baileys sidecar (events were injected into the real webhook exactly
as the sidecar sends them); the sender-side drop of a reply that was queued before the contact was listed (covered by
unit and Postgres tests only).

## 7. UX consequences the FE must surface (write these as copy, not trivia)
1. **Existing conversations are not deleted.** If the contact already chatted with Luran, adding them stops new
   messages but the old conversation stays in the inbox until the tenant deletes it. Say so on the add form. (Pause-only
   rows created by the owner typing to them are removed automatically.)
2. **The tenant will not see an excluded contact's messages in Luran at all**, by design. Tell them to read those on
   their phone.
3. **Exclusion also stops human replies from the dashboard** to that contact (a queued or agent message to an excluded
   contact is dropped silently by the sender). If a tenant removes the entry the contact is normal again.
4. **Add contacts straight after connecting a number.** WhatsApp sometimes identifies a contact only by an internal id
   and does not tell Luran the phone number on the first message. In that case the **first** message from that contact
   may be treated like a customer's and answered; Luran learns the internal id from the first message that does carry the
   number, and ignores that contact from then on. A contact who already chatted and whose number Luran saw is ignored
   at once when added. Suggest, in the "connect a number" flow, a final step "Add personal
   contacts Luran must never reply to", and show the same hint on the list screen. (Known gap, not hidden: see the SOP.)
5. **It is per number and by phone number, not by name.** Group chats are never answered by Luran anyway (the sidecar
   drops them), so there is nothing to exclude there.
6. **Numbers need a country code.** Use a country selector or a placeholder like `+234 801 234 5678`; surface the
   `invalid_phone` message as is.
7. The tenant's own connected number cannot be added (`own_number`).

## 8. What the dashboard PR needs
Per connected number (inside the existing WhatsApp connection detail):
1. A **"Contacts Luran ignores"** section: empty state (explain 1 and 2 of section 7), list rows (`phone_masked`,
   `label`, added date, Remove button), pagination or "load more" using `limit/offset/total`, count `total`.
2. **Add one**: phone input (with country hint) + optional label (60 chars) -> `POST …/exclusions`; map errors from
   section 3 inline; on 409 show "already on the list", on 201 prepend the returned entry.
3. **Add many**: a textarea (one per line, optional `number, label`), split client-side, send in chunks of up to 200 to
   `…/bulk`; show `added / already_excluded` and list `rejected` rows by line number (the response gives the index).
4. **Remove**: confirm, then `DELETE`; on 204 drop the row, on 404 refresh the list.
5. Gating: hide or disable add/bulk when `features.whatsapp.platform_available && enabled` is false (list/remove still work).
6. A one-line nudge in the connect-a-number flow (section 7.4).
No new endpoint is needed beyond section 2.

## 9. Verification table
| Behaviour | Verified |
|---|---|
| Add (+ spaces normalised, mask, label), list, page, remove, remove again 404 | live [05, 08, 09, 21, 22] |
| Duplicate 409, national format 422, garbage 422, own number 422, empty bulk 422, `limit=0` 422 | live [02, 03, 04, 06, 10, 12] |
| Bulk partial success counts and `rejected` indexes | live [11] |
| Other tenant 404 on list/add/delete; unknown connection 404 | live [14-17] |
| Add 403 `whatsapp_not_enabled` after a platform revoke; list still 200 | live [18-20] |
| Excluded contact: webhook drops (phone ref, LID + digits, LID alone, voice note, owner typing), zero rows, zero sends | live (`pipeline-evidence.json`) |
| Worker drops an event queued before the entry was added | live (`worker-race-evidence.json`) |
| Real customer on the same number still answered; answered again after removal | live (`pipeline-evidence.json`) |
| `exclusion_limit` 409, `whatsapp_misconfigured` 503, bulk > 500 shape | NOT live (unit tests / code only) |
| Sender drops a reply queued before the contact was listed; agent takeover | NOT live (unit + Postgres tests) |
| Real phone / real Baileys sidecar / LID without resolvable number | NOT live (fake sidecar only) |
