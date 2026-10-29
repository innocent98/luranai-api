# FE integration guide: WhatsApp is plan-driven (default-on) + capacity (2026-10-29)

Audience: the dashboard (tenant app and platform admin). **This guide supersedes** the "platform grant" statements in
`fe-integration-guide-whatsapp-channel.md` (gating, section on `whatsapp_not_enabled`, the capacity text) and in
`fe-integration-guide-entitlements.md` (trap 1, "needs the platform grant too"). Those documents carry a pointer here.

Every body below was copied from a real response of a locally running app (Postgres 15, Redis 7, the fake sidecar,
LLM stubbed), captured by `scripts/capture_whatsapp_default_on.py` into
`docs/fe-integration-captures/whatsapp-default-on/` (file numbers in brackets). Placeholder values: pairing code
`FAKE…` comes from the fake sidecar; the operator address is `ops-placeholder@example.com`.

## 1. What changed, in the FE's terms
* Until now WhatsApp was off for every tenant until a Luran admin granted it. **Now every tenant has WhatsApp from
  its plan**: Trial 1 number, Lite 1, Professional 2, Growth 5, Enterprise unlimited. Nobody has to be granted anything.
* There are now **two independent switches**:
  1. the tenant's plan/override: `features.whatsapp.enabled` in `GET /api/v1/tenant/entitlements`;
  2. a platform-wide kill switch (`WHATSAPP_ENABLED`, off in production until the production sidecar exists):
     the new `features.whatsapp.platform_available`.
  **`enabled` does not include the kill switch.** While `platform_available` is `false` every tenant has
  `enabled: true` but cannot use WhatsApp (see capture 18/19). Gate the whole WhatsApp section on
  `platform_available && enabled`.
* The platform admin "WhatsApp grant" control keeps working, but it now means **override of the plan** (section 5).

## 2. `GET /api/v1/tenant/entitlements`: the `whatsapp` entry
The only change to the payload: the `whatsapp` object gains `platform_available` (boolean, always present). Every
other feature object is unchanged. Captured whatsapp entries, one per plan, kill switch on:

| Plan | Capture | `whatsapp` entry |
|---|---|---|
| trial | [02] | `{"enabled": true, "min_tier": "trial", "limit": 1, "used": 0, "platform_available": true}` |
| lite | [03] | `{"enabled": true, "min_tier": "trial", "limit": 1, "used": 0, "platform_available": true}` |
| professional | [04] | `{"enabled": true, "min_tier": "trial", "limit": 2, "used": 0, "platform_available": true}` |
| growth | [05] | `{"enabled": true, "min_tier": "trial", "limit": 5, "used": 0, "platform_available": true}` |
| trial, after one number was created | [08] | `{"enabled": true, "min_tier": "trial", "limit": 1, "used": 1, "platform_available": true}` |
| professional, admin revoked | [13] | `{"enabled": false, "min_tier": "trial", "limit": 2, "used": 0, "platform_available": true}` |
| professional, revoke undone | [17] | `{"enabled": true, "min_tier": "trial", "limit": 2, "used": 0, "platform_available": true}` |
| growth, API with the kill switch off | [18] | `{"enabled": true, "min_tier": "trial", "limit": 5, "used": 0, "platform_available": false}` |

Rules for the FE:
* `min_tier` is `"trial"` for every plan: **never show an upgrade prompt for WhatsApp as a feature**. `limit` > `used`
  means another number can be added; `limit` reached means "your plan allows N numbers", and the upsell is *more numbers*
  (higher plan), not WhatsApp itself. `limit: null` is unlimited (Enterprise).
* `enabled: false` with `limit > 0` and `platform_available: true` means **Luran turned WhatsApp off for this account**
  (an admin revoke, [13]) or the account is lapsed (trial expired, canceled, past due: same as every other feature).
  Show "WhatsApp is not available on your account. Contact support." Do not show an upgrade button.
* `platform_available: false`: show "WhatsApp is not available yet" (or hide it). This is the state of production until
  the owner switches WhatsApp on.

## 3. `POST /api/v1/whatsapp/connections`
* A tenant on any plan creates a number with no grant [06] (trial tenant, `201`):
  ```json
  {"id": "a600a82b-0e9c-47b8-a8a8-2f764c9a5330", "status": "awaiting_pair", "pairing_code": "FAKE6928", "expires_in_s": 180}
  ```
* Over the plan's number limit [07] `409`: `{"code": "connection_limit", "message": "Your plan allows 1 WhatsApp number(s)."}`
* Account revoked by an admin [14] `403`: `{"code": "whatsapp_not_enabled", "message": "WhatsApp is not enabled for this account."}`
* Kill switch off [19] `403`: **the same body** `{"code": "whatsapp_not_enabled", "message": "WhatsApp is not enabled for this account."}`.
  The two cases cannot be told apart from the error; use `platform_available` and `enabled` from the entitlements call.
  `GET /whatsapp/connections` still answers `200` with the tenant's rows when the kill switch is off ([20], `[]` there).
* **Capacity [11] `503`, now stable:**
  ```json
  {"code": "capacity_exceeded", "message": "WhatsApp is temporarily at capacity. Please try again later."}
  ```
  This one text is used for the backend's own cap, for a `logged_out` re-pair at capacity, and for the sidecar's own
  capacity refusal. It is the platform's cap, not the tenant's quota. Show it as a calm "try again later" state, with a
  retry button; do **not** route it to billing/upgrade; do not auto-retry in a loop. Disconnecting any tenant's idle
  number frees a slot. The older capture `whatsapp-channel/17-create-capacity-exceeded.json` has the previous wording
  ("at capacity right now"); the `code` is unchanged, so key logic on `code`, never on the text.

## 4. Capacity alert (operators, no FE work)
When live connections are one short of the limit or at 80% of `WHATSAPP_MAX_CONNECTIONS` (whichever comes first), or the
sidecar reports memory at 80% of its refuse threshold, the platform operators in `PLATFORM_ALERT_EMAILS` get one email
per condition per 6 hours. Capture [10] is the email after the second connection (limit 2 in that run) and [21] is the
cron run: first run emails `sidecar_memory`, the second run inside 6 hours sends nothing, and the Redis keys expire in
21600 s. Tenants never see or receive this.

## 5. Platform admin: what the "WhatsApp grant" control means now
`PUT /api/v1/platform/tenants/{tenant_id}/capabilities` with `{"can_use_whatsapp": <bool>}` (and optionally
`max_whatsapp_numbers`) is unchanged on the wire. What it means:
* It records an **override** for that field on that tenant (`override_flags.can_use_whatsapp = true`, capture [15]).
  An override beats the plan.
* **`false` = revoke.** Captures [12]-[14]: the response has `"can_use_whatsapp": false`, the tenant's entitlements show
  `enabled: false`, create answers `403 whatsapp_not_enabled`. The revoke **survives** a plan change, a downgrade,
  a promotional grant starting or ending, and a platform tier override.
* **`true` = force on.** Because every plan includes WhatsApp, `true` changes nothing for a tenant that was never revoked;
  its use is *undoing a revoke* [16]-[17]. The override key stays recorded afterwards ([15] shows it after the revoke).
* Relabel the control "WhatsApp access" (on = follows plan/allowed, off = revoked). `max_whatsapp_numbers` is the
  per-tenant number allowance, as before.
* The platform capabilities response does **not** expose `override_flags`, so the admin UI cannot show "this is an
  override" and there is no way to clear one (only to set it to true or false). Ask backend before relying on it.

## 6. Traps
1. `enabled` ignores the kill switch; `platform_available` is the kill switch. Read both.
2. The same `403 whatsapp_not_enabled` body covers an admin revoke and the kill switch (create is also refused with it
   when the account has no WhatsApp allowance); use the entitlements call to decide the message.
3. `used` counts all of the tenant's connection rows including unpaired ones, as before.
4. The feature string on public plans changes from "WhatsApp (beta, on request): N number(s)" to
   "WhatsApp (beta): N number(s)" ("unlimited numbers" for Enterprise). Do not hard-code the old text. On an
   environment where the migration has run but the seed has not, hand-edited plans keep their old strings.

## 7. Verification table
| Behaviour | Status |
|---|---|
| Entitlements `whatsapp` entry per plan, revoked, restored, kill switch on and off | **verified live** [02]-[05], [08], [13], [17], [18] |
| Plan-only tenant (never granted) creates a number; over-limit 409 | **verified live** [06], [07] |
| Admin revoke and restore through the platform PUT, create 403 | **verified live** [12]-[16] |
| Kill switch off: create 403, list 200 | **verified live** [18]-[20] (second API process) |
| Capacity 503 body | **verified live** [11] (backend cap); the sidecar-originated and re-pair variants use the same constant, **automated tests only** |
| Capacity alert email, cron, 6-hour dedupe | **verified live** [10], [21] (local SMTP sink, fake sidecar `/health`, threshold lowered to 25% for the cron run because the fake reports 30% memory) |
| Migration backfill (plan value, override true/false kept, NULL flags) and downgrade/upgrade/upgrade | **verified live** [22] on a real migrated database |
| Revoke surviving a downgrade, a promotional grant and a tier override | **automated tests only** |
| `enabled: false` for a lapsed account (trial expired, canceled, past due) | **automated tests only** (resolver rule shared with every other feature) |
| Real Baileys sidecar, a real phone, real SMTP provider, production data | **not exercised** |
