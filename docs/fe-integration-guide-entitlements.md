# FE integration guide: plan entitlements

Backend: branch `feature/plan-entitlements-backend` (PR to `develop`). SOP: `docs/sop/plan_entitlements.md`.
Contract this implements: the 2026-10-28 entitlements contract (matrix + `GET /tenant/entitlements` + the 403 gate error).

**Every body below is copied from `docs/fe-integration-captures/entitlements/` (one file per call), captured by `scripts/capture_entitlements.py` against a locally running API on a freshly migrated and seeded Postgres.** Placeholders: tenant ids, emails and tokens are throwaway local values; treat them as examples. Where something could not be exercised live it is marked **NOT LIVE**.

## 1. What this is

Plans now decide what a tenant can use, not just how much. The backend exposes the decision in one place: `GET /api/v1/tenant/entitlements`. Read it once after login (and again after an upgrade, a downgrade or a promotion), keep it in app state, and use it to:

* show a lock + an upgrade prompt on a feature the plan does not include,
* skip showing options that cannot work,
* turn a 403 `feature_not_in_plan` from a save into the same upgrade prompt.

The matrix (what each tier includes; the endpoint is the source of truth, do not hard-code this):

| Feature key | Trial | Lite | Professional | Growth | Enterprise |
|---|---|---|---|---|---|
| `sales_close` (meeting booking + quotes) | yes | no | yes | yes | yes |
| `crm_notifications` (Slack + CRM webhook) | yes | no | yes | yes | yes |
| `screen_recording` | yes | no | yes | yes | yes |
| `ratings_export` (admin ratings breakdown + CSV) | yes | no | yes | yes | yes |
| `ai_actions` (max) | 2 | 2 | 15 | 40 | unlimited |
| `lookup_chains`, `webhooks` | no | no | yes | yes | yes |
| `analytics` | no | yes | yes | yes | yes |
| `whatsapp` numbers (plan-driven since 2026-10-29, no grant needed) | 1 | 1 | 2 | 5 | unlimited |
| `voice` | platform grant only, never from a plan |||||

Never gated (no lock, no check): human handoff, tickets, agent app, image upload, rating **collection**, lead capture/scoring/email notice, the web widget. Lite is 15,000 naira/month (`monthly_price_kobo` 1500000), annual 15000000 kobo.

## 2. `GET /api/v1/tenant/entitlements`

Auth: any authenticated tenant user (admin or support agent). It always answers for the caller's own tenant; there is no parameter to ask for another one. It is **not** blocked after the trial expires (no 402), so the upgrade view can render.

Unauthenticated:
```json
{
  "detail": "Could not validate credentials"
}
```
_(from `02-entitlements-unauthenticated.json`; status 401)_

Lite tenant (admin):
```json
{
  "tier": "lite",
  "subscription_status": "active",
  "features": {
    "sales_close": {
      "enabled": false,
      "min_tier": "professional"
    },
    "crm_notifications": {
      "enabled": false,
      "min_tier": "professional"
    },
    "screen_recording": {
      "enabled": false,
      "min_tier": "professional"
    },
    "ratings_export": {
      "enabled": false,
      "min_tier": "professional"
    },
    "lookup_chains": {
      "enabled": false,
      "min_tier": "professional"
    },
    "webhooks": {
      "enabled": false,
      "min_tier": "professional"
    },
    "analytics": {
      "enabled": true,
      "min_tier": "lite"
    },
    "ai_actions": {
      "enabled": true,
      "min_tier": "trial",
      "limit": 2,
      "used": 0
    },
    "whatsapp": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 1,
      "used": 0
    },
    "voice": {
      "enabled": false,
      "min_tier": null
    }
  }
}
```
_(from `07-entitlements-lite.json`; status 200)_

Support agent on the same Lite tenant gets the same body: ```json
{
  "tier": "lite",
  "subscription_status": "active",
  "features": {
    "sales_close": {
      "enabled": false,
      "min_tier": "professional"
    },
    "crm_notifications": {
      "enabled": false,
      "min_tier": "professional"
    },
    "screen_recording": {
      "enabled": false,
      "min_tier": "professional"
    },
    "ratings_export": {
      "enabled": false,
      "min_tier": "professional"
    },
    "lookup_chains": {
      "enabled": false,
      "min_tier": "professional"
    },
    "webhooks": {
      "enabled": false,
      "min_tier": "professional"
    },
    "analytics": {
      "enabled": true,
      "min_tier": "lite"
    },
    "ai_actions": {
      "enabled": true,
      "min_tier": "trial",
      "limit": 2,
      "used": 0
    },
    "whatsapp": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 1,
      "used": 0
    },
    "voice": {
      "enabled": false,
      "min_tier": null
    }
  }
}
```
_(from `19-entitlements-lite-support-agent.json`; status 200)_

Trial tenant:
```json
{
  "tier": "trial",
  "subscription_status": "trialing",
  "features": {
    "sales_close": {
      "enabled": true,
      "min_tier": "professional"
    },
    "crm_notifications": {
      "enabled": true,
      "min_tier": "professional"
    },
    "screen_recording": {
      "enabled": true,
      "min_tier": "professional"
    },
    "ratings_export": {
      "enabled": true,
      "min_tier": "professional"
    },
    "lookup_chains": {
      "enabled": false,
      "min_tier": "professional"
    },
    "webhooks": {
      "enabled": false,
      "min_tier": "professional"
    },
    "analytics": {
      "enabled": false,
      "min_tier": "lite"
    },
    "ai_actions": {
      "enabled": true,
      "min_tier": "trial",
      "limit": 2,
      "used": 0
    },
    "whatsapp": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 1,
      "used": 0
    },
    "voice": {
      "enabled": false,
      "min_tier": null
    }
  }
}
```
_(from `03-entitlements-trial.json`; status 200)_

Professional tenant:
```json
{
  "tier": "professional",
  "subscription_status": "active",
  "features": {
    "sales_close": {
      "enabled": true,
      "min_tier": "professional"
    },
    "crm_notifications": {
      "enabled": true,
      "min_tier": "professional"
    },
    "screen_recording": {
      "enabled": true,
      "min_tier": "professional"
    },
    "ratings_export": {
      "enabled": true,
      "min_tier": "professional"
    },
    "lookup_chains": {
      "enabled": true,
      "min_tier": "professional"
    },
    "webhooks": {
      "enabled": true,
      "min_tier": "professional"
    },
    "analytics": {
      "enabled": true,
      "min_tier": "lite"
    },
    "ai_actions": {
      "enabled": true,
      "min_tier": "trial",
      "limit": 15,
      "used": 0
    },
    "whatsapp": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 2,
      "used": 0
    },
    "voice": {
      "enabled": false,
      "min_tier": null
    }
  }
}
```
_(from `21-entitlements-professional.json`; status 200)_

Lite tenant with one manual platform override (`sales_close` switched on by a platform admin):
```json
{
  "tier": "lite",
  "subscription_status": "active",
  "features": {
    "sales_close": {
      "enabled": true,
      "min_tier": "professional"
    },
    "crm_notifications": {
      "enabled": false,
      "min_tier": "professional"
    },
    "screen_recording": {
      "enabled": false,
      "min_tier": "professional"
    },
    "ratings_export": {
      "enabled": false,
      "min_tier": "professional"
    },
    "lookup_chains": {
      "enabled": false,
      "min_tier": "professional"
    },
    "webhooks": {
      "enabled": false,
      "min_tier": "professional"
    },
    "analytics": {
      "enabled": true,
      "min_tier": "lite"
    },
    "ai_actions": {
      "enabled": true,
      "min_tier": "trial",
      "limit": 2,
      "used": 0
    },
    "whatsapp": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 1,
      "used": 0
    },
    "voice": {
      "enabled": false,
      "min_tier": null
    }
  }
}
```
_(from `26-entitlements-lite-with-override.json`; status 200)_

Trial that has expired (still 200, no 402; **every feature is `enabled: false`** because the account is lapsed, `tier` stays `trial`, and `subscription_status` tells you why):
```json
{
  "tier": "trial",
  "subscription_status": "trial_expired",
  "features": {
    "sales_close": {
      "enabled": false,
      "min_tier": "professional"
    },
    "crm_notifications": {
      "enabled": false,
      "min_tier": "professional"
    },
    "screen_recording": {
      "enabled": false,
      "min_tier": "professional"
    },
    "ratings_export": {
      "enabled": false,
      "min_tier": "professional"
    },
    "lookup_chains": {
      "enabled": false,
      "min_tier": "professional"
    },
    "webhooks": {
      "enabled": false,
      "min_tier": "professional"
    },
    "analytics": {
      "enabled": false,
      "min_tier": "lite"
    },
    "ai_actions": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 2,
      "used": 0
    },
    "whatsapp": {
      "enabled": false,
      "min_tier": "trial",
      "limit": 1,
      "used": 0
    },
    "voice": {
      "enabled": false,
      "min_tier": null
    }
  }
}
```
_(from `28-entitlements-trial-expired.json`; status 200)_

### Field rules

* `features.<key>.enabled`: use this to decide lock vs unlock.
* `features.<key>.min_tier`: lowest tier from which the feature stays included; show it in the upgrade prompt ("Available on Professional"). `null` means a platform grant only (voice): never show an upgrade prompt, show "contact support" if you show anything.
* `ai_actions` and `whatsapp` have `limit` (**`null` = unlimited**, never 0) and `used`. A feature object for a flag-only feature has **no** `limit`/`used` keys at all (absent, not null).
* Field order inside an object is not guaranteed.

### Traps (read these)

1. **SUPERSEDED 2026-10-29 (WhatsApp is now plan-driven; read `docs/fe-integration-guide-whatsapp-default-on.md`): `whatsapp.enabled` follows the plan/override and the entry gained `platform_available` (the global kill switch). The paragraph below describes the old grant model and the captures above predate it.** ~~**`whatsapp.enabled` is the platform grant, not the plan.** In every capture above `whatsapp.limit` is 1 or 2 but `enabled` is `false`: no grant is set on these tenants. `enabled=false` with `limit>0` means "not granted to your account; contact support", **not** "upgrade". The plan only decides how many numbers once granted. (The contract sketch showed `enabled: true` for a Lite tenant; the backend reports `true` only when the grant exists.)~~
2. **Trial shows features as enabled with `min_tier: "professional"`.** The trial includes the sales features, Slack/CRM, recording and ratings export so a trial user can try them; `min_tier` says where they stay after the trial. Show "included in your trial; keep it with Professional", not a lock.
3. **`analytics` is off on Trial** and the Analytics page answers 403 `"Analytics access not enabled for this tenant"` (a different, older error shape, see section 4). Use `analytics.enabled` to lock the sidebar item.
4. **Overrides can exceed the tier.** Lite + override: `sales_close.enabled` is `true` while `min_tier` stays `professional`. Never derive `enabled` from `tier`.
5. **Lapsed accounts have nothing enabled.** When `subscription_status` is `trial_expired`, `canceled` or `past_due` (the same statuses the API answers 402 for) every feature reads `enabled: false` whatever the tier, so routes without their own 402 check (ratings, sales config, the public meeting picker) cannot be used for free. Show the upgrade/renew view from `subscription_status`, not a per-feature lock.
6. A promotional grant changes `tier` in this response to the granted tier for its duration (and back afterwards; once a grant's end date has passed it stops counting immediately, before the nightly expiry job reverts the tenant). Re-fetch when a grant starts or ends.

## 3. The gate error (HTTP 403, new gates only)

Saving a gated option the plan does not include is rejected. **HTTP 403, flat body, not wrapped in `detail`, and not 402**: do not route it to the trial-expired upgrade redirect that handles 402.

Shape (all four keys always present; `min_tier` is a tier name, `null` only for grant-only features):
```json
{"code": "feature_not_in_plan", "feature": "<key>", "min_tier": "<tier>", "message": "<plain sentence>"}
```

Real examples, all from a Lite tenant:

Enable meeting booking: `PATCH /api/v1/sales/config`
```json
{
  "meeting_booking_enabled": true
}
```
```json
{
  "code": "feature_not_in_plan",
  "feature": "sales_close",
  "min_tier": "professional",
  "message": "Your plan does not include meeting booking and quotes. Upgrade to Professional to use it."
}
```
_(from `08-sales-config-lite-enable-booking.json`; status 403)_

Enable quotes: same body (`feature: "sales_close"`):
```json
{
  "code": "feature_not_in_plan",
  "feature": "sales_close",
  "min_tier": "professional",
  "message": "Your plan does not include meeting booking and quotes. Upgrade to Professional to use it."
}
```
_(from `09-sales-config-lite-enable-quotes.json`; status 403)_

Set a Slack webhook URL: `PATCH /api/v1/sales/config`
```json
{
  "slack_webhook_url": "https://hooks.slack.com/services/T111/B111/other-placeholder"
}
```
```json
{
  "code": "feature_not_in_plan",
  "feature": "crm_notifications",
  "min_tier": "professional",
  "message": "Your plan does not include Slack and CRM notifications. Upgrade to Professional to use it."
}
```
_(from `10-sales-config-lite-enable-slack-url.json`; status 403)_

Turn the CRM channel on: `PATCH /api/v1/sales/config`
```json
{
  "notify_channels": {
    "email": true,
    "crm": true
  }
}
```
```json
{
  "code": "feature_not_in_plan",
  "feature": "crm_notifications",
  "min_tier": "professional",
  "message": "Your plan does not include Slack and CRM notifications. Upgrade to Professional to use it."
}
```
_(from `11-sales-config-lite-enable-crm-channel.json`; status 403)_

Ratings breakdown (admin list) and CSV export, same shape with `feature: "ratings_export"`:
```json
{
  "code": "feature_not_in_plan",
  "feature": "ratings_export",
  "min_tier": "professional",
  "message": "Your plan does not include the ratings breakdown and CSV export. Upgrade to Professional to use it."
}
```
_(from `13-ratings-list-lite-admin.json`; status 403)_
```json
{
  "code": "feature_not_in_plan",
  "feature": "ratings_export",
  "min_tier": "professional",
  "message": "Your plan does not include the ratings breakdown and CSV export. Upgrade to Professional to use it."
}
```
_(from `14-ratings-export-lite-admin.json`; status 403)_

### What the sales config save rejects

`PATCH /api/v1/sales/config` returns the gate error when the save **switches a gated option on**:

* `meeting_booking_enabled` or `quotation_enabled` going from false to true -> `sales_close`;
* a non-empty `slack_webhook_url` / `crm_webhook_url` that differs from what is stored, or `notify_channels.slack` / `.crm` going from off to on -> `crm_notifications`.

Not rejected: turning things **off**, re-sending an option that is already on (so a tenant who was downgraded can still save other settings), and every ungated field (email notifications, in-app, currency, lead capture, qualification questions...):
`PATCH /api/v1/sales/config`
```json
{
  "notification_email": "sales@example.com",
  "currency": "NGN"
}
```
```json
{
  "success": true,
  "data": {
    "id": "39e2ff39-8fea-46f1-ab07-1f651899e156",
    "tenant_id": "072ea545-94a6-4421-ae28-91e2e4eed161",
    "agent_enabled": true,
    "lead_capture_enabled": true,
    "meeting_booking_enabled": false,
    "quotation_enabled": false,
    "crm_provider": null,
    "calendar_provider": null,
    "notify_channels": {
      "email": true,
      "slack": false,
      "in_app": false,
      "crm": false
    },
    "notification_email": "sales@example.com",
    "slack_webhook_url": "https://hooks.slack.com/services/T000/B000/placeholder",
    "crm_webhook_url": null,
    "trigger_intents": "... (unchanged, omitted here)",
    "qualification_questions": "... (unchanged, omitted here)",
    "pricing_tiers": "... (unchanged, omitted here)",
    "currency": "NGN",
    "annual_discount_pct": 0,
    "availability": "... (unchanged, omitted here)",
    "created_at": "2026-10-28T09:29:03.635012+00:00",
    "updated_at": "2026-10-28T09:29:04.304936+00:00"
  }
}
```
_(from `12-sales-config-lite-ungated-save.json`; status 200)_

A rejected save changes nothing. The same save succeeds on Trial and Professional:
```json
{
  "success": true,
  "data": {
    "id": "7de33daf-c152-45a9-ab7e-019a0aa413f9",
    "tenant_id": "7abd2070-1e44-4ef9-9dab-e376d2d720cf",
    "agent_enabled": false,
    "lead_capture_enabled": true,
    "meeting_booking_enabled": true,
    "quotation_enabled": true,
    "crm_provider": null,
    "calendar_provider": null,
    "notify_channels": {
      "email": true,
      "slack": true,
      "crm": true
    },
    "notification_email": null,
    "slack_webhook_url": null,
    "crm_webhook_url": null,
    "trigger_intents": "... (unchanged, omitted here)",
    "qualification_questions": "... (unchanged, omitted here)",
    "pricing_tiers": "... (unchanged, omitted here)",
    "currency": "NGN",
    "annual_discount_pct": 0,
    "availability": "... (unchanged, omitted here)",
    "created_at": "2026-10-28T09:29:06.057030+00:00",
    "updated_at": "2026-10-28T09:29:06.057030+00:00"
  }
}
```
_(from `22-sales-config-professional-enable-all.json`; status 200)_

### Options that are still on after a downgrade or promotion ending

The stored setting is **not** rewritten. At runtime the gated behaviour is silently skipped and the end customer never sees an error. Live capture, Lite tenant whose stored config still has booking on (public widget endpoints, no auth):

`GET /api/v1/chat/meetings/availability?conversation_id=6e6b82d1-f353-437a-9ccf-9cf0fb27f09c&days=2`
```json
{
  "success": true,
  "data": {
    "slots": []
  }
}
```
_(from `17-public-meeting-availability-lite.json`; status 200)_
`POST /api/v1/chat/meetings`
```json
{
  "conversation_id": "6e6b82d1-f353-437a-9ccf-9cf0fb27f09c",
  "scheduled_at": "2030-01-07T10:00:00Z",
  "visitor_timezone": "UTC"
}
```
```json
{
  "success": false,
  "error": {
    "code": "BOOKING_DISABLED",
    "message": "Meeting booking is not enabled"
  }
}
```
_(from `18-public-meeting-book-lite.json`; status 403)_

So the visitor sees an empty picker / the same `BOOKING_DISABLED` a tenant with booking switched off produces. In the dashboard, show these settings as locked when `sales_close.enabled` is false, even if the stored value is `true`.

## 4. Endpoints that changed behaviour

| Endpoint | On a plan without the feature |
|---|---|
| `PATCH /sales/config` | 403 gate error when switching a gated option on (section 3) |
| `GET /reviews/session` | **tenant admin** looking at ratings other than their own: 403 `ratings_export`. Support agents / viewers (own ratings only) and an admin passing their own `rated_agent_id` still get 200 |
| `GET /reviews/session/export` (**new**, tenant admin only) | 403 `ratings_export`. With the feature: `text/csv`, header `created_at,score,comment,rated_party,rated_agent_name,source,ticket_number,customer_label`, up to 10,000 rows, comments that start with `=`, `+`, `-` or `@` are prefixed with `'` |
| `GET /analytics/agents`, `GET /analytics/agents/{id}` | still 200 (analytics is a separate gate); the per-agent `ratings_count`, `average_rating`, `csat_percent` are `0`, `null`, `null` unless the plan has `ratings_export`, or an agent reads their own row |
| `GET /widget/config` (admin) | `can_use_screen_recording` is the plan decision |
| public widget config + `POST /chat/video/sign` / `register` + chat send with a video | recording is off: existing 403 `screen_recording_disabled` on the video endpoints, `allow_screen_recording: false` in the public config |
| `POST /whatsapp/connections` | `connection_limit` (409) now uses the plan's number of lines; `whatsapp_not_enabled` (403) now means an admin revoke or the global kill switch (was: no platform grant until 2026-10-29) |
| AI action create | existing "Automation action limit reached" error, limit now plan-driven (Lite 2) |
| `GET /analytics/*` | unchanged 403 string for tenants whose plan has no analytics |

Lite tenant, admin analytics agent table (ratings columns zeroed):
```json
[
  {
    "agent_id": "1b7ac70d-a295-47bd-83a3-17881e5de7db",
    "agent_name": "Demo Owner",
    "email": "ent-a-0f575e@example.com",
    "current_assigned_tickets": 0,
    "tickets_by_status": {},
    "total_tickets_resolved": 0,
    "ratings_count": 0,
    "average_rating": null,
    "csat_percent": null
  }
]
```
_(from `15-analytics-agents-lite.json`; status 200)_

Lite tenant, admin widget settings:
```json
{
  "tenant_id": "072ea545-94a6-4421-ae28-91e2e4eed161",
  "company_name": "Demo Bakery a db3576",
  "widget_primary_color": "#2563eb",
  "widget_accent_color": "#10b981",
  "widget_logo_url": null,
  "widget_greeting_message": "Hi! How can we help you today?",
  "widget_position": "bottom-right",
  "widget_show_unread_badge": true,
  "widget_allow_file_upload": true,
  "widget_allow_screen_recording": true,
  "can_use_screen_recording": false
}
```
_(from `16-widget-config-lite-admin.json`; status 200)_

Lite support agent, own ratings (stays open): ```json
{
  "ratings": [],
  "total": 0,
  "average_score": null,
  "csat_percent": null,
  "page": 1,
  "page_size": 50
}
```
_(from `20-ratings-list-lite-agent-self-view.json`; status 200)_

Professional admin, ratings list and CSV (empty tenant, so header row only):
```json
{
  "ratings": [],
  "total": 0,
  "average_score": null,
  "csat_percent": null,
  "page": 1,
  "page_size": 50
}
```
_(from `23-ratings-list-professional-admin.json`; status 200)_
```text
'created_at,score,comment,rated_party,rated_agent_name,source,ticket_number,customer_label\r\n'
```
_(from `24-ratings-export-professional-admin.json`; status 200, content-type `text/csv; charset=utf-8`)_

## 5. Downgrade: new conflicts

`POST /subscription/downgrade` validates before changing anything. Three new violation fields join the existing ones (`knowledge_sources`, `storage_usage`, `team_members`, `url_sources`, `flows`, `automation_actions`): `sales_close`, `crm_notifications` (both captured below) and `whatsapp_numbers` (**NOT LIVE**: needs a linked WhatsApp number; covered by a backend test, same shape as the others):

`POST /api/v1/subscription/downgrade`
```json
{
  "tier": "lite"
}
```
```json
{
  "detail": {
    "error": "data_exceeds_limits",
    "violations": [
      {
        "field": "sales_close",
        "current_value": 2,
        "allowed_value": 0,
        "difference": 2,
        "action_required": "Turn off meeting booking and quotes in the sales agent settings before downgrading"
      },
      {
        "field": "crm_notifications",
        "current_value": 2,
        "allowed_value": 0,
        "difference": 2,
        "action_required": "Turn off Slack and CRM notifications in the sales agent settings before downgrading"
      }
    ],
    "message": "Your current usage exceeds the new tier's limits"
  }
}
```
_(from `25-downgrade-blocked-by-gated-options.json`; status 400)_

Show each `action_required` string and block the downgrade until the tenant fixes it. `sales_close` / `crm_notifications` use `current_value` = number of switched-on options (booking+quotes = 2 at most; slack+crm = 2 at most) and `allowed_value` 0.

## 6. Public pricing

`GET /api/v1/subscription/plans` (public) now returns the Lite price of 15,000 naira and feature strings that match what is enforced. Enterprise is not listed (unchanged). Lite and Professional entries (trimmed to the changed fields):
```json
[
  {
    "tier": "lite",
    "monthly_price_kobo": 1500000,
    "annual_price_kobo": 15000000,
    "monthly_price_naira": 15000.0,
    "annual_price_naira": 150000.0,
    "features": [
      "20 knowledge sources",
      "50MB storage",
      "1,000 conversations/month",
      "3 team members",
      "3 flows",
      "2 automation actions",
      "500 automation executions/month",
      "AI chat automation",
      "URL scraping",
      "Analytics dashboard",
      "WhatsApp (beta, on request): 1 number",
      "Full support"
    ],
    "capabilities": {
      "can_use_sales_close": false,
      "can_use_crm_notifications": false,
      "can_use_screen_recording": false,
      "can_export_ratings": false,
      "max_whatsapp_numbers": 1,
      "max_automation_actions": 2
    }
  },
  {
    "tier": "professional",
    "monthly_price_kobo": 4000000,
    "annual_price_kobo": 40000000,
    "monthly_price_naira": 40000.0,
    "annual_price_naira": 400000.0,
    "features": [
      "50 knowledge sources",
      "200MB storage",
      "5,000 conversations/month",
      "10 team members",
      "10 flows",
      "15 automation actions",
      "5,000 automation executions/month",
      "AI chat automation",
      "Lookup chains",
      "Up to 3 webhooks per flow",
      "URL scraping",
      "Analytics dashboard",
      "Meeting booking and quotes",
      "Slack and CRM notifications",
      "Screen recording",
      "Ratings breakdown and CSV export",
      "WhatsApp (beta, on request): 2 numbers",
      "Full support"
    ],
    "capabilities": {
      "can_use_sales_close": true,
      "can_use_crm_notifications": true,
      "can_use_screen_recording": true,
      "can_export_ratings": true,
      "max_whatsapp_numbers": 2,
      "max_automation_actions": 15
    }
  }
]
```
_(from `01-public-plans.json`; status 200)_

> **2026-10-29:** the plan captures above show the feature string `WhatsApp (beta, on request): N number(s)`; it is now `WhatsApp (beta): N number(s)` and each plan's `capabilities` also carries `can_use_whatsapp: true`.

### Monthly conversation limits (2026-10-29)

The monthly conversation limit of each plan went down. Everything below is copied from the live captures in `docs/fe-integration-captures/conversation-limits/` (local app, migrated to head, plans seeded; re-run `scripts/capture_conversation_limits.py` to re-derive).

| Plan | `capabilities.max_conversations_per_month` | Feature string | Was |
|---|---|---|---|
| Trial | `100` | `"100 conversations/month"` | 200 |
| Lite | `1000` | `"1,000 conversations/month"` | 2,000 |
| Professional | `5000` | `"5,000 conversations/month"` | 10,000 |
| Growth | `10000` | `"10,000 conversations/month"` | 25,000 |
| Enterprise | not listed (unlimited, stored as `null`) | n/a | unchanged |

_(from `01-public-plans.json`, status 200)_

* **Never hardcode these numbers in the dashboard or the pricing page.** Read `capabilities.max_conversations_per_month` and the `features` strings from `GET /api/v1/subscription/plans`; they changed once and a platform admin can change them again. Search your code for `200`, `2000`, `10000`, `25000` next to "conversation".
* A tenant's own limit is on its capabilities, not on the plan: a new Trial tenant reads `"max_conversations_per_month": 100` (`03-platform-tenant-trial-after-sync.json`), a Growth tenant synced from its plan reads `10000` (`02-platform-tenant-growth-after-sync.json`). A tenant a platform admin edited by hand can read any number (next bullet).
* **Existing tenants move too.** The migration lowers every tenant still on its plan's old number and leaves hand-edited ones alone. A tenant already above its new limit this month is **not** cut off from what it has: existing conversations stay and can continue; only starting a NEW counted conversation is refused, with the unchanged `402` body below, until the 1st of next month (UTC).
* **UX to surface:** show the limit and the used count where the tenant can see them, and when the used count is at or above the limit say plainly "new conversations are paused until the 1st of next month; open conversations are not affected; upgrade to raise the limit". Do not describe it as a failure on our side. The `402` body and the widget handling are in `docs/fe-integration-guide-conversation-counting.md` (shape unchanged; only the numbers inside it differ).
* **Downgrade warning:** a downgrade whose usage this month is above the target plan's new limit still succeeds and returns the warning string `Your current monthly conversations (N) exceed the new tier's limit (L). Limit will reset at start of next month.` in `warnings` (verified by test against the new catalog numbers; not captured live because the Paystack leg of a downgrade is not exercised locally). Show it before the user confirms.
* **Platform admin, hold one tenant at its old number:** `PUT /api/v1/platform/tenants/{id}/capabilities` with `{"max_conversations_per_month": 25000}` returns 200 with the full capabilities (`04-platform-hold-growth-tenant-at-25000.json`). The edit is recorded as `override_flags: {"max_conversations_per_month": true}`. It survives an upgrade or activation sync (`06-...-upgrade-or-activation-sync.json`: still 25000). It does **not** survive a downgrade, a promotional grant starting or ending, or a platform tier change that applies the tier capabilities: the stored value goes back to the plan's 10000 and the flag is cleared (`07-...-downgrade-or-promo-sync.json`). The dashboard should tell an admin this when they set the override.
* **Platform pricing helper** `POST /api/v1/platform/pricing/enterprise/compare` (platform admin): with Growth capped at 10,000 and Enterprise volume pricing starting at 25,000 there is a gap. Real responses: 10,000 returns `"recommended_tier": "growth"` (`07-pricing-compare-10000.json`); 15,000 returns `"recommended_tier": "custom"` with `"reason": "Volume (15,000) is above the Growth limit (10,000) and below the Enterprise minimum (25,000)"` (`08-pricing-compare-15000.json`); 30,000 returns `"recommended_tier": "enterprise"` (`09-pricing-compare-30000.json`). The `custom` value is new; render it as "needs a custom quote".

`capabilities` in each plan now also carries `can_use_sales_close`, `can_use_crm_notifications`, `can_use_screen_recording`, `can_export_ratings`, `max_whatsapp_numbers` (`null` = unlimited) and the audit limits. The prices only change in the database when the migration + seed run on that environment: see the SOP for the order of operations and the Paystack step.

## 7. Behaviour the user will notice (surface these)

* A tenant whose trial expired, or whose subscription is canceled or past due, loses every gated feature at once (section 2, trap 5).
* A tenant that moves to Lite **loses** meeting booking, quotes, Slack/CRM, screen recording and the ratings breakdown immediately, even though their stored settings stay on. Say so in the downgrade confirmation (the backend only blocks the downgrade while the sales options and Slack/CRM channels are still switched on).
* When a promotional grant ends the tenant is back on its previous tier and the same applies.
* Lite's AI-action limit is 2 (it was 5). A tenant with more actions than the new limit is blocked from downgrading until they delete some.
* The Lite price is a database value, not per-tenant: it applies to anyone on Lite once the migration/seed run. There are no Lite or Professional paying tenants in prod today (2026-10-27 aggregates), so nobody is repriced.

## 8. Verification table

| Behaviour | Verified |
|---|---|
| `GET /tenant/entitlements` for trial, lite, professional, override, expired trial, support agent, 401 | live (captures 02-03, 07, 19, 21, 26, 28) |
| Gate error body and 403 status on sales config, ratings list, ratings export | live |
| Ungated sales config save on Lite, gated save on Trial and Professional, override unlock | live |
| Public meeting picker / booking on a Lite tenant with booking still on | live |
| Analytics agent table ratings zeroed on Lite; agent self-view stays open | live (empty ratings; the non-empty and CSV-with-rows cases are tests only) |
| Downgrade blocked with `sales_close` + `crm_notifications` violations | live |
| Public plans: Lite price, generated feature strings | live |
| Public plans: new conversation limits and strings (100 / 1,000 / 5,000 / 10,000) | live (`conversation-limits/01`) |
| Admin override holds a tenant at 25,000 through an upgrade sync, is cleared by a downgrade/promo sync | live (DB state, `conversation-limits/05-07`) |
| Pricing compare: growth / custom / enterprise recommendations | live (`conversation-limits/07-09`) |
| Downgrade `warnings` string when usage is above the new limit | automated test only; **NOT LIVE** (Paystack leg not exercised) |
| `402 conversation_limit_reached` at the new numbers | shape live in the conversation-counting guide; **not re-captured** at 100/1,000/5,000/10,000 |
| CSV export with data, formula neutralising, tenant isolation of the export | automated tests only (Postgres), not live |
| Chat: no `offer_meeting` / `quote` on a plan without `sales_close`, sales prompt told booking is off | automated tests with a stubbed LLM only; **NOT LIVE** (no real LLM was called) |
| Slack/CRM not dispatched and queued jobs not sent after a downgrade | automated tests only; **NOT LIVE** (no real Slack/CRM endpoint) |
| Screen-recording gate on the public widget config, `video/sign`, `video/register`, chat send | automated tests only; **NOT LIVE** (Cloudinary not configured locally) |
| WhatsApp `connection_limit` from the plan (Lite 1, Professional 2, Enterprise unlimited) | automated tests with the fake sidecar only; **NOT LIVE** |
| Downgrade `whatsapp_numbers` violation | automated test only; **NOT LIVE** |
| Promotional grant changes `tier`/features in the response | automated tests only; the capture script does not create grants |
| Paystack charges the new Lite amount | **NOT LIVE**, not exercised; see the SOP owner-action list |
