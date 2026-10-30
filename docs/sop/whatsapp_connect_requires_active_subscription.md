# SOP: WhatsApp connect requires an active subscription, 2026-10-30

## What shipped
Branch `fix/whatsapp-connect-requires-active-subscription` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges). Production untouched.

`POST /api/v1/whatsapp/connections` (link a number) and `POST /api/v1/whatsapp/connections/{id}/pairing-code` (re-pair) now depend on `verify_subscription_active`, the same dependency the knowledge, flows, API-key, user and ticket endpoints use. A tenant whose trial expired, whose subscription is canceled or past due, or whose paid period ended gets the existing **HTTP 402** instead of a pairing code.

## Why
The WhatsApp launch ceiling is 10 numbers platform-wide, first come first served (`WHATSAPP_MAX_CONNECTIONS`). WhatsApp is default-on for every plan, and 10 of 13 production tenants are `trial_expired`. `create_connection` checked the kill switch, `can_use_whatsapp`, consent, rate limits, the per-tenant number cap and the platform cap, but **nothing about the subscription**: `_require_enabled` deliberately reads `entitlements.value("can_use_whatsapp")`, not `enabled("whatsapp")`, which is the check that returns false for a lapsed account (see `docs/sop/whatsapp_default_on.md`, "lapsed-account behaviour"). A lapsed tenant could therefore link a number, take a slot, and get no AI replies at all because inbound already blocks them.

Reproduced first with failing tests (6 of 10 new tests failed with `201`/`200` instead of `402`; the 4 that must keep working passed).

## How
| Decision | Why / rejected alternative |
|---|---|
| Reuse `verify_subscription_active` as a FastAPI dependency on the two token-minting endpoints | Smallest change, identical 402 bodies to every other endpoint, and it also flips a lazily-expired trial/subscription status. Rejected: a new check inside `WhatsAppService` (second source of truth for "lapsed", and a new error shape the dashboard would need). Rejected: switching `_require_enabled` to `enabled("whatsapp")` (would return the 403 `whatsapp_not_enabled` for a billing problem and also change inbound, which is intentionally on `value()`). |
| Gate create **and** re-pair | Re-pairing a `logged_out` connection mints a pairing code and claims a live slot again (`new_pairing_code` has its own capacity check), so leaving it open would be the same hole. |
| Leave list, get, patch, delete, consent and the exclusion endpoints open | A lapsed tenant must be able to see its numbers and free a slot (delete) without upgrading. |
| Promotional grants need no special case | A grant writes `subscription_status="active"` on the tenant (`promotional_grant_service.py:120`), so it passes; an ended grant is back to the previous (lapsed) status. Covered by a test. |

Status values are the real ones in `verify_subscription_active` (`app/api/deps.py`): stored `trial_expired`, `canceled`, `past_due` -> `subscription_inactive`; `trialing`/`trial` past `trial_ends_at` -> `trial_expired`; `active` past `subscription_ends_at` -> `subscription_expired`.

## What's involved
* `app/api/v1/endpoints/whatsapp.py`: import + `_subscription_check` dependency on `create_connection` and `new_pairing_code`.
* `tests/whatsapp/test_connections_api.py`: 10 new tests (3 stored lapsed statuses, trial past end date, active past end date, re-pair, list/delete stay open, trialing/active/promo-granted still connect).
* FE guide `docs/fe-integration-guide-whatsapp-connect-subscription-gate.md`, captures `docs/fe-integration-captures/whatsapp-connect-subscription-gate/`.
* No migration, no config, no new env var.

## Already-linked numbers of a lapsed tenant (investigated, no change needed)
* **Inbound already stops answering.** `whatsapp_inbound._plan_blocks_conversation` returns true for stored `trial_expired`/`canceled`/`past_due` and for a trial past its end date; the message is stored on the existing conversation (never opens one), nothing is said to the customer, and `last_alert_at` is debounced (`_plan_limit`, result `"plan_limit"`). Tests: `tests/whatsapp/test_inbound.py::test_expired_subscription_blocks_non_text_and_the_paused_store_path`, `::test_plan_limit_stores_the_message_when_a_conversation_exists`.
* **Gap (small, not changed):** that check does not look at `subscription_ends_at`. An `active` tenant whose paid period ended keeps being answered until something flips its status (the staff API flips it lazily on the next 402 check; with no staff traffic nothing does). `entitlements.is_lapsed` already encodes the full rule and could replace the inline check; left alone because it changes inbound behaviour for a case the task did not ask about.
* **Owner-typed messages** from a lapsed tenant's phone still pause the chat (`handle_outbound_external` is gated only by `_tenant_may_use`). Harmless.

## Capacity: do lapsed tenants' numbers still count?
**Yes.** `_live_count` counts every connection with status `awaiting_pair`, `connecting`, `connected` or `reconnecting`, regardless of the tenant's subscription. Nothing logs a lapsed tenant's sessions out; only the account-deletion purge calls `logout_tenant_connections`. So a number linked before the lapse (or before this fix ships) keeps its slot and its sidecar session (memory) while the AI stays silent. No cleanup job was added (out of scope). Operator options: delete the connection as a platform operator, or a follow-up job that logs out sessions of tenants lapsed for N days.

## Verification
| Check | Result |
|---|---|
| New tests (fail first, then pass) | 10 new; `tests/whatsapp/test_connections_api.py` 46 passed |
| Real response bodies | captured in-process from the real FastAPI app (test client, sqlite, fake sidecar) into `docs/fe-integration-captures/whatsapp-connect-subscription-gate/`; **not** captured from a deployed server |
| `ruff check app/`, `black --check app/` (project venv, ruff 0.8.6 / black 24.10.0) | clean |
| `tests/whatsapp` as a whole, locally | 353 passed; 12 failed and 17 errored only where Redis or Postgres is needed and neither runs on this machine (`test_capacity_alert.py`, `test_exclusion_postgres.py`, one real-Redis limiter test); CI has them |
| NOT exercised | a real tenant with a stored `trial_expired` status in staging/prod, the dashboard rendering of the 402 |

## Operate / roll back
Revert the commit; no data change. After deploy, a lapsed tenant on the connect page sees a 402 on submit (see the FE guide for what the dashboard shows today).

## Follow-ups
* Dashboard: map the three 402 `error` values on the connect page (today they show the generic "Something went wrong on our side"). Details in the FE guide section "Dashboard".
* Make `_plan_blocks_conversation` use `entitlements.is_lapsed` so an expired paid period also stops answers without waiting for a status flip.
* Decide whether lapsed tenants' sessions should be logged out after N days to release slots and sidecar memory.
