# SOP: Idempotent `charge.success` webhook, 2026-10-28

## What shipped
`WebhookHandlerService._handle_charge_success` now returns `{"status": "already_processed"}` and changes nothing when the payment transaction for the event's `reference` is already `success`. PR: `fix/charge-success-idempotent` -> `develop`.

## Why
Paystack redelivers webhooks, and two deliveries can arrive in parallel. The handler was not idempotent: every delivery re-ran the tier upgrade (resetting the subscription period), appended another `SubscriptionHistory` upgrade row and overwrote `paid_at`. Prod has had no payments yet, so this is fixed before the first one. The referral program (`docs/superpowers/specs/2026-10-28-referral-program-design.md`) also builds money-path code on this handler and needs "one payment, one effect".

## How
| Decision | Detail |
|---|---|
| Row lock | The transaction lookup is `SELECT ... FOR UPDATE`, so a parallel delivery waits. The first delivery's `upgrade_subscription` commit writes `status="success"` and releases the lock; the waiting delivery re-reads (READ COMMITTED), sees `success` and takes the early return. |
| Early return | After the not-found check, before any write. It calls `commit()` only to end the transaction and release the lock (nothing is pending). `rollback()` was rejected because it wipes the test fixture's savepoint setup; with no pending mutation both are equivalent in production. |
| Scope | Nothing else in the handler changed. No other code path sets a transaction to `success`. |

## What's involved
`app/services/webhook_handler_service.py` (`_handle_charge_success`), `tests/services/test_webhook_charge_success_idempotent.py` (first delivery unchanged, replay does not re-run the upgrade, replay adds no history row, lookup issues `FOR UPDATE`).

## Verification
New tests: RED before the change (3 failed), 4 passed after. `pytest tests -k webhook` passes. Lint and format clean with the pinned ruff 0.8.6 and black 24.10.0. Not exercised: two real concurrent database connections (the lock test asserts the emitted SQL only).

## Operate / roll back
No migration, no config. Revert the commit to restore the old behaviour.

## Follow-ups
- A delivery whose tier upgrade raised is still stored as `status="success"` with `error_message` set (the handler swallows the error so Paystack gets a 200). A replay no longer retries that upgrade: such rows need a manual fix (look for `error_message LIKE 'Subscription upgrade failed%'`).
- The history row is committed separately from the upgrade (pre-existing); a crash between them leaves an upgrade without a history row that a replay will not backfill.
- Optional: a two-connection concurrency test.
