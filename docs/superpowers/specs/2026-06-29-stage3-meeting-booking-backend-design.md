# Stage 3 — AI Sales Agent · Slice 3 "Inline Meeting Picker" — Backend Design

**Date:** 2026-06-29
**Repo:** `luran` (FastAPI · SQLAlchemy async · Pydantic · Alembic · pytest · OpenAI)
**Paired FE specs:** `luran-chat/docs/superpowers/specs/2026-06-29-stage3-meeting-picker-widget-design.md` (widget picker) · `luran-dashboard/docs/superpowers/specs/2026-06-29-stage3-meeting-availability-config-design.md` (availability config)
**Builds on:** Slice 1 "Leads Core" (`Lead`, `LeadService`, `TenantSalesAgentConfig`) and Slice 2 "Chat-Widget Sales" (`app/sales/intent.py`, `SALES_RESPONSE_SCHEMA`, sales-mode generation in `rag_service.py`, sales orchestration in `chat.py`).
**PRD:** `docs/luran_prd.md` §3.4 (Meeting Booking Flow), §3.6 (Sales config), acceptance §2.3 ("Meeting booking flow completes end-to-end").
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Slice 3 lets the customer-facing chat agent **offer an inline date/time picker** when it detects demo/call intent, and **book a native meeting** linked to the Slice 1 lead — with no redirect and no external calendar account. Per the locked slice order: 1 Leads core ✅ → 2 Chat-widget sales ✅ → **3 Meeting booking (this)** → 4 Quotation → 5 Notifications+CRM.

**Decision (brainstorm):** availability is **native** — the tenant defines bookable windows in their Sales config; open slots = windows − already-booked meetings. A native `Meeting` row is the source of truth. **Google Calendar / Calendly sync is deferred to a follow-up slice (3b)** — `calendar_provider` stays reserved and unused. This keeps the slice thin, ships the full PRD §3.4 inline UX now, and removes external dependencies from the upcoming live E2E.

**In scope (BE):**
- New `Meeting` model + Alembic migration; link to `Lead.meeting_id`.
- Availability config on `TenantSalesAgentConfig` (bookable windows, slot length, buffer, tenant timezone).
- Piggyback `signals.offer_meeting` on the Slice 2 structured output; surface `meta.offer_meeting` to the widget gated by `meeting_booking_enabled`.
- Two public widget endpoints: compute availability, book a slot (with transactional double-booking prevention).

**Out of scope (later slices):** Google/Calendly sync (3b); visitor confirmation email, rep email, in-app alert (all Slice 5); reschedule/cancel UI; quotation (Slice 4).

**Constraint (PRD acceptance §291):** Sales Agent does not interrupt pure support flows — when sales mode is off or intent is `SUPPORT`, no picker is offered and the existing path is unchanged.

---

## 2. Data model

New table `meetings` (`app/db/models/meeting.py`):

| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid, indexed, not null | tenant scope |
| `lead_id` | uuid FK → leads.id, not null | owning lead |
| `conversation_id` | str/uuid, indexed | chat session that booked it |
| `scheduled_at` | timestamptz (UTC) | absolute meeting start |
| `duration_min` | int | copied from config slot length at booking |
| `visitor_timezone` | str | IANA tz captured from the browser (for display/email later) |
| `use_case` | text, nullable | optional one-line "what to cover" |
| `status` | str enum `scheduled` \| `cancelled` | lowercase, serialized lowercase (matches LeadStatus convention) |
| `created_at` | timestamptz | |

`Lead.meeting_id` (existing nullable uuid) is set to the booked meeting. One active meeting per lead in this slice (re-booking replaces/links the latest; see §5).

**Migration:** additive only — new `meetings` table + availability columns on the sales-config table. No destructive changes.

---

## 3. Availability config

Extend `TenantSalesAgentConfig` (reuse the existing row; `meeting_booking_enabled` and `calendar_provider` already exist). Add an `availability` JSON column (nullable) with shape:

```json
{
  "weekdays": [1,2,3,4,5],          // ISO weekday ints, Mon=1
  "start": "09:00",                  // tenant-local clock time
  "end": "17:00",
  "slot_minutes": 30,
  "buffer_minutes": 15,
  "tenant_timezone": "Africa/Lagos"  // IANA
}
```

Defaults on first create: Mon–Fri, 09:00–17:00, 30-min slots, 15-min buffer, `tenant_timezone` defaulting to a sensible value (e.g. `UTC` until the tenant sets it). `meeting_booking_enabled` default stays `False` (tenant opts in). The dashboard config page (paired FE) edits these.

---

## 4. Piggyback: offering the picker

Extend `SALES_RESPONSE_SCHEMA` (`app/sales/intent.py`): add `offer_meeting: bool` to the `signals` object (keep `additionalProperties:false` valid — add to the property list). The sales directive instructs the model to set `offer_meeting=true` when the visitor wants a demo/call (typically `DEMO_REQUEST`, or clear call intent) AND it has enough context to suggest scheduling.

In `chat.py` sales orchestration, after generation:
- Compute `offer_meeting = bool(signals.offer_meeting) and config.meeting_booking_enabled`.
- If true, set `response.meta["offer_meeting"] = true`. (No picker data inline — the widget fetches slots via the availability endpoint.)
- Gate is server-side authoritative: even if the model sets the flag, a tenant with booking disabled never gets `meta.offer_meeting`.

LLM/parse failure → no `offer_meeting`, plain reply, no side effects (same degradation as Slice 2).

---

## 5. Endpoints (public widget path)

Mounted on the same public widget router as the chat endpoint (tenant resolved from the widget session, same as Slice 2):

**`GET …/meetings/availability?conversation_id=…&days=N`**
- Resolves tenant + lead from the conversation; 404/empty if no lead or booking disabled.
- Computes open slots over the next `N` days (default e.g. 14): enumerate config windows in `tenant_timezone`, step by `slot_minutes`, apply `buffer_minutes`, drop past slots, drop slots colliding with existing `scheduled` meetings for the tenant.
- Returns each slot as `{ "start_utc": ISO8601, "label_tz": "WAT" }` — the widget renders the local-time label from `start_utc` using the visitor's browser tz; `tenant_timezone` need not leak. Response is the standard `{success, data:{slots:[...]}}` envelope.

**`POST …/meetings` `{conversation_id, scheduled_at (UTC ISO), visitor_timezone, use_case?}`**
- Validates the slot still falls inside a config window and is **not already booked** — done inside a transaction (re-query + insert) so two simultaneous bookings can't grab the same slot; the loser gets a 409 with a "slot just taken" message.
- Creates the `Meeting` (status `scheduled`, `duration_min` from config), sets `lead.meeting_id`, persists.
- Returns `{success, data:{ meeting:{ scheduled_at, duration_min, label_tz } }}` for the widget confirmation card. **No emails/notifications** (Slice 5).
- Tenant-scoped, validated `scheduled_at` is in the future.

---

## 6. Safety & invariants

- All times stored UTC; windows interpreted in `tenant_timezone`; visitor display derived client-side.
- Double-booking prevented by transactional re-check at POST (DB is source of truth, not the GET snapshot).
- Picker offered only when `meeting_booking_enabled` AND sales mode on AND model signals it — server-side gate is authoritative.
- Tenant isolation throughout (meeting carries `tenant_id`; availability/booking scoped to the conversation's tenant).
- `SUPPORT`/non-trigger turns never offer a meeting.
- Booking is idempotent-friendly: re-POST for a lead that already has a future meeting links/returns the existing one rather than duplicating (defensive against double-submit).

---

## 7. Testing (pytest)

- **Availability** (`tests/sales/test_meeting_availability.py`): slot generation respects weekdays/start/end/slot length/buffer; past slots excluded; booked slots excluded; tz conversion correct (tenant-TZ window → UTC slots); booking disabled → empty/404.
- **Booking** (`tests/api/v1/test_meeting_booking.py`): valid slot → `Meeting` created, `lead.meeting_id` set, status `scheduled`; double-book same slot → second gets 409, no duplicate row; slot outside window rejected; past slot rejected; cross-tenant isolation; double-submit returns existing meeting.
- **Piggyback** (extend Slice 2 generation tests): `offer_meeting` parsed; surfaced to `meta.offer_meeting` only when `meeting_booking_enabled`; gate off → no meta even if model sets it; sales-mode-off path unchanged.
- Green `poetry run pytest`; no regression to chat/support/Slice 1–2 suites.

---

## 8. Files

**New:** `app/db/models/meeting.py`, Alembic migration (meetings table + availability column), `app/services/meeting_service.py` (availability computation + booking), `app/api/v1/endpoints/meetings.py` (or extend the widget router), `app/schemas/meeting.py`, `tests/sales/test_meeting_availability.py`, `tests/api/v1/test_meeting_booking.py`.
**Modify:** `app/sales/intent.py` (`offer_meeting` signal + directive), `app/services/rag_service.py` (schema already threaded — verify signal passes through), `app/api/v1/endpoints/chat.py` (surface `meta.offer_meeting`), `app/db/models/sales_agent_config.py` (`availability` column + defaults), `app/schemas/conversation.py` (document `meta.offer_meeting`).

---

## 9. Acceptance (maps to PRD §2.3 / §3.4)

1. On demo intent with booking enabled, the chat response carries `meta.offer_meeting` so the widget can render the picker.
2. Availability endpoint returns correct open slots (windows − booked − past), tenant-TZ-defined, UTC-encoded.
3. Booking creates a native `Meeting`, links `lead.meeting_id`, returns a confirmation payload; double-booking is impossible (transactional).
4. Booking disabled or sales-mode off → no offer, no endpoints effect, existing behavior unchanged.
5. All times correct across timezones (stored UTC, displayed visitor-local).
6. No emails/notifications fire in this slice (Slice 5 owns them). All new tests green; existing suite green.
