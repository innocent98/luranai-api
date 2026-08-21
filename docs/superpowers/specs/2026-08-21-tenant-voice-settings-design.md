# Tenant-facing Voice Settings — Design

> **Status:** Approved 2026-08-21. Feeds `writing-plans`.
> **Repos:** `luran` (BE, branch off `develop`) · `luran-dashboard` (FE, branch off `staging`)
> **Related:** `2026-08-15-voice-chat-backend-design.md` (the feature this configures),
> `2026-08-19-spike-1-findings.md` (how a call actually reaches us).
> **Reviewer:** senior session reviews each slice before merge; no self-merge to a deploy branch.

**Goal.** Give a tenant admin a settings page for the voice chat their own widget offers:
turn it on or off, choose the assistant's voice, and see how many of their monthly minutes
they have used.

---

## PRD note — read this before citing a feature number

`docs/luran_prd.md` (last modified 2026-06-17) contains **no mention of voice**. Voice chat was
brainstormed 2026-08-15, two months later. There is no feature number to cite, and inventing one
would be worse than the gap.

**This design proposes adding two PRD entries**, in the existing `Feature 3.x` style:

- **Feature 3.9 — Voice Chat.** The customer speaks to the assistant inside the widget; the
  assistant answers in audio. Same pipeline as text (RAG, KB, flows, tools, lead scoring, actions).
- **Feature 3.10 — Voice Settings & Usage.** The tenant controls whether their widget offers voice,
  which voice it uses, and can see their consumption against their allotment.

Writing those entries is in scope for this work. Until they exist, cite this spec.

---

## 1. The decision everything else hangs off: where the preference lives

**Tenant preference goes on `TenantAIConfig` (`tenant_ai_configs`), NOT on `TenantCapabilities`.**

`TenantCapabilities` means *what the platform grants*. `can_use_voice` and
`max_voice_minutes_per_month` are entitlements, set by a super-admin
(`PUT /api/v1/platform/tenants/{id}/capabilities`, PR #80) and soon by plan tier. A tenant
preference is not an entitlement.

Keeping them on separate tables makes **"a tenant can never self-grant a paid, per-minute
capability"** structural rather than a rule enforced by a code path someone could later change.
The tenant-facing write endpoint has no reference to `TenantCapabilities` at all.

Secondary benefit: `_update_capabilities_for_tier` (`subscription_service.py:63`) iterates
`plan_config.items()` and assigns onto `TenantCapabilities`. A preference living on a different
table cannot be clobbered by a plan re-sync, now or if someone later adds voice keys to the plan
config (which item 3 of the queue will do).

### New columns on `tenant_ai_configs`

| Column | Type | Default | Meaning |
|---|---|---|---|
| `voice_enabled` | `bool NOT NULL` | `true` | Tenant's own on/off for their widget |
| `voice_id` | `varchar NULL` | `NULL` | Chosen ElevenLabs voice id; `NULL` = platform default |

`voice_enabled` defaults **true** so that granting a tenant voice makes it work immediately —
the admin grant is the deliberate act, and a tenant who wants it off opts out.

**Migration note.** Verify the real alembic head with `alembic heads` before writing
`down_revision`. A guessed head silently forks the chain; that mistake has already occurred once
on this feature (see `project_voice_chat`).

---

## 2. Gate order

Today (`gating.py:101` `VoiceGate.check`): **flag → minute cap → concurrency**.

Becomes: **flag → tenant preference → minute cap → concurrency**.

The tenant-preference denial **reuses the existing reason
`VoiceDenialReason.FLAG_DISABLED = "voice_disabled"`**. Reasons cross the API boundary and the
shipped widget already handles that value with correct copy ("Voice isn't available right now —
let's keep going in text"). Adding a new reason string would require a widget change for no
user-visible benefit.

Ordering matters: the preference check sits **after** the grant so that a tenant without a grant
is never told anything about their preference, and **before** the cap so a disabled tenant costs
no Redis read.

**In-progress calls when a tenant toggles off:** existing calls finish; no new session mints.
This falls out of the gate being mint-time only. It is the correct behaviour — cutting a live
caller off mid-sentence to honour a settings change would be worse — and is stated here so it is
not later "fixed".

---

## 3. Backend endpoints

Both live in `app/api/v1/endpoints/tenant.py`, using the existing tenant-self pattern
(`Depends(get_current_tenant_admin)`), alongside `get_my_capabilities` and `get_my_ai_config`.

### `GET /api/v1/tenant/voice/settings`

One call returns everything the page needs:

```json
{
  "granted": true,
  "enabled": true,
  "voice_id": "<el-voice-id>",
  "minutes_used": 142.5,
  "minutes_limit": 500,
  "available_voices": [
    {"id": "<el-voice-id>", "name": "…", "description": "…", "preview_url": "https://…"}
  ]
}
```

- `granted` — `TenantCapabilities.can_use_voice` (read-only here).
- `enabled` — `TenantAIConfig.voice_enabled`.
- `minutes_used` — `VoiceMeter.get_monthly_minutes(tenant_id)`; reads the existing Redis key
  `voice:minutes:{tenant_id}:{YYYY-MM}`. **No new storage.**
- `minutes_limit` — `max_voice_minutes_per_month`; `null` means unlimited and the UI must render
  that as "Unlimited", never as a bar at 0%.
- `available_voices` — the curated list (§4).

**The GET always returns every field, whatever `granted` is.** The endpoint reports state; the
page decides what to render from it (§5). Omitting fields when `granted` is false would make the
response shape conditional, which is harder to type on the client and gives the page less to work
with than it needs — it still shows usage and lets a tenant pre-configure.

`minutes_used` must degrade rather than fail: if Redis is unreachable, return `null` and let the
page show "Usage unavailable". A settings page that 500s because a cache is down is worse than
one missing a number.

### `PUT /api/v1/tenant/voice/settings`

Accepts **only** `{"enabled": bool, "voice_id": str | null}`. It has no code path that reads or
writes `TenantCapabilities`.

- `voice_id` must be a member of the curated list, else `422`. Never accept an arbitrary id — an
  unvetted or nonexistent voice would fail at mint time, far from where the mistake was made.
- Writing `enabled` is allowed regardless of `granted`. A tenant may pre-configure before a grant
  arrives; the gate still refuses to mint. Rejecting the write would leak entitlement state into
  a settings form for no benefit.

---

## 4. Curated voice list

~8 ElevenLabs voices, defined as a backend constant and served by the GET. Rationale: predictable
quality and cost, no EL API call on page load, and each voice vetted for support conversations.

Shape per entry: `id`, `name`, `description` (e.g. "Warm, British, measured"), `preview_url`
(ElevenLabs' public preview mp3, so the page auditions without proxying audio through us).

**The actual voice ids are NOT specified here.** They must be selected from the account's real
ElevenLabs library during implementation and pasted in verbatim. Fabricating ids that look
plausible would produce a list that fails only at runtime. Pick voices spanning gender, accent,
and pace; record which and why in the SOP.

`voice_id = NULL` means "platform default" — the mint path already works with no voice override,
so a tenant who never chooses gets the agent's configured voice.

---

## 5. Frontend — `luran-dashboard`

New sub-page `app/(dashboard)/dashboard/settings/voice/page.tsx` plus a nav entry in
`settings/layout.tsx`, following the existing sub-page pattern (company, ai, subscription,
api-keys, password, automation).

**It must be its own page, not a section of `settings/ai`.** That page owns `brand_voice`
(tone / formality / greeting) — an unrelated feature that shares a word. Putting voice-chat there
is the single most likely source of user and developer confusion in this work. The nav label is
**"Voice Chat"**, never "Voice".

### States

| Condition | Render |
|---|---|
| `granted: false` | Explain voice isn't enabled for the plan and who to contact. Controls hidden, not merely disabled. |
| `granted: true` | On/off toggle, voice picker with preview, usage bar. |
| `enabled: false` | Controls stay visible and editable; usage bar shown; a note that the widget currently offers text only. |
| `minutes_limit: null` | "Unlimited" — no bar. |
| `minutes_used: null` | "Usage unavailable" — the rest of the page still works. |

### Usage bar

Neutral below 80%, amber at ≥80%, red at 100%. At 100%, copy states plainly that voice is paused
until the month resets and who to contact for more. The tenant should never first learn of the cap
from a customer hitting a dead Talk button.

Brand: Luran v2 — ink `#0E0D2A`, cream `#FAFAF7`, burnt-amber `#B4541E` accent, Geist. This is the
tenant dashboard, so it follows the dashboard's existing conventions where they differ.

---

## 6. Testing

**Backend.** The gate's new ordering is the load-bearing property: a granted-but-tenant-disabled
tenant must be denied with `voice_disabled`, and an ungranted tenant must be denied *by the grant*
regardless of preference. Both need tests that fail if the two checks are swapped or either is
removed — not merely that some denial occurred.

`PUT` must be proven unable to alter `TenantCapabilities`: post a body containing
`can_use_voice` and assert the grant is unchanged. The endpoint has no such field, so this is a
regression guard against someone adding one.

Reject an out-of-list `voice_id` with `422`.

**Frontend.** Each of the five states above renders correctly; the 80% and 100% thresholds
switch styling; `minutes_limit: null` renders "Unlimited" and not a full bar.

**A standing warning from this feature's history.** Across the voice track, the recurring defect
was tests that passed while being structurally incapable of detecting their target — most starkly,
tests proving we parsed three request fields ElevenLabs never sends. For every test here, state
what broken implementation it would let through.

---

## 7. Slices

1. **BE data + gate** — two columns, migration, `VoiceGate.check` preference step, tests.
2. **BE endpoints + curated list** — `GET`/`PUT`, curated voices, usage read, tests.
3. **FE page** — settings sub-page, nav entry, five states, usage bar.

Each slice: SDD, its own PR, senior review before merge. SOP under each repo's `docs/sop/` and the
master checklist updated on ship.

---

## 8. Out of scope

- **Pricing, tiers, and allotment sizing** — that is the subscription brainstorm (queue item 2),
  which this design deliberately does not pre-empt. This page displays whatever allotment exists.
- **Plan-tier auto-grant** — queue item 3.
- **Threshold emails** — considered and dropped for this slice; needs a scheduled job, a template,
  and per-tenant send-once state. Revisit with the subscription work.
- **Per-conversation usage breakdown** — monthly total only.
- **Tenant-uploaded custom voices** — EL supports voice cloning; out of scope and a separate
  cost and consent conversation.
