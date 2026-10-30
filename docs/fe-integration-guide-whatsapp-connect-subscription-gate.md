# FE integration guide: WhatsApp connect is refused for a lapsed account (402)

Every body below is copied from `docs/fe-integration-captures/whatsapp-connect-subscription-gate/` (a real response of the app, captured in-process with the test client and the fake sidecar, 2026-10-30). Placeholder values: ids and the `FAKE####` pairing code are fake.

## 1. What changed
`POST /api/v1/whatsapp/connections` and `POST /api/v1/whatsapp/connections/{id}/pairing-code` now answer **402** when the tenant's trial has expired or its subscription is not active. Before, they answered 201/200 and the number took a platform slot while the AI stayed silent. Listing, reading, patching and deleting connections still work for a lapsed tenant. The 402 is checked **after** the admin check (a non-admin still gets 403) and **before** the WhatsApp rules (consent, number limit, capacity).

## 2. The three bodies (FastAPI `detail` wrapper; the key is `error`, not `code`)
| Tenant state | `detail.error` | Capture |
|---|---|---|
| Stored status `trial_expired`, `canceled` or `past_due` | `subscription_inactive` | [01] [02] [03], re-pair [06] |
| Status `trialing` but `trial_ends_at` has passed (status flips to `trial_expired` as a side effect) | `trial_expired` | [04] |
| Status `active` but `subscription_ends_at` has passed (flips to `canceled`) | `subscription_expired` | [05] |

```json
{"detail": {"error": "subscription_inactive", "message": "Subscription trial_expired. Please update payment.", "status": "trial_expired", "upgrade_url": "/dashboard/billing"}}
```
```json
{"detail": {"error": "trial_expired", "message": "Your 30-day trial has expired. Please upgrade to continue using Luran.", "trial_ended": "2026-10-29T20:20:29.858817+00:00", "upgrade_url": "/dashboard/upgrade", "subscription_tier": "trial"}}
```
```json
{"detail": {"error": "subscription_expired", "message": "Your subscription has ended. Please reactivate to continue using Luran.", "subscription_ended": "2026-10-29T20:20:29.862860+00:00", "upgrade_url": "/dashboard/billing", "subscription_tier": "trial"}}
```
A tenant that is `trialing` inside its 30 days, `active`, or promo-granted (status `active`) still gets the normal `201` [07]:
```json
{"id": "c1b44b54-f21e-4cce-a634-b72fa13fde16", "status": "awaiting_pair", "pairing_code": "FAKE2932", "expires_in_s": 180}
```

## 3. Traps
1. **Most expired accounts get `subscription_inactive`, not `trial_expired`.** Production tenants whose trial lapsed already have the stored status `trial_expired`, which goes through the `subscription_inactive` branch (its `message` literally reads "Subscription trial_expired. Please update payment."). Branch on all three `error` values, never only on `trial_expired`.
2. **The error key is `detail.error`, WhatsApp's own errors use top-level `code`.** `toApiError` in `app/lib/whatsapp-api.ts` reads `inner.code`, so for these bodies `code` is `null` and the user sees the generic "Something went wrong on our side. Please try again in a moment." (`whatsAppErrorMessage` default). The `message` field is available and plain, but the `subscription_inactive` one is not friendly ("Subscription trial_expired...").
3. `upgrade_url` differs per body (`/dashboard/billing` vs `/dashboard/upgrade`); the app's own upgrade page is `/dashboard/settings/subscription/upgrade` (see `api-client.ts`). Do not follow `upgrade_url` blindly.
4. The global 402 interceptor skips `/whatsapp/` URLs on purpose, so nothing redirects: the connect page must show the state itself.

## 4. Dashboard (not changed in this task)
Suggested one-line mapping in `toApiError`: `const code = typeof inner.code === "string" ? inner.code : typeof inner.error === "string" ? inner.error : null;`, plus in `whatsAppErrorMessage` one case for `trial_expired`, `subscription_inactive`, `subscription_expired`: "Your trial or subscription has ended. Upgrade to link a WhatsApp number." with a link to the app's upgrade page. Better still: hide or disable the connect form when `GET /tenant/entitlements` reports `whatsapp.enabled: false` for a lapsed account (the resolver already returns false for lapsed statuses), so the 402 is only a backstop.

## 5. Verification table
| Behaviour | Status |
|---|---|
| 402 bodies for stored `trial_expired`, `canceled`, `past_due` on create | **verified** [01]-[03] (in-process real app, not a deployed server) |
| 402 for trial past end date and for active past end date | **verified** [04], [05] |
| 402 on re-pair | **verified** [06] |
| 201 for a trialing tenant inside its trial | **verified** [07]; `active` and promo-granted: **automated tests only** |
| List and delete still allowed for a lapsed tenant | **automated tests only** |
| What the dashboard shows today | **read from code** (`whatsapp-api.ts`), not run in a browser |
| Deployed staging/prod behaviour | **not exercised** |
