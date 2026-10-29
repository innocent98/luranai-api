# Referral program: live captures

Real requests and responses from the referral backend, recorded by `scripts/capture_referral.py` against a locally running app on a scratch Postgres database and Redis (nothing real: no production, no Paystack, no email).

Each file is `{description, note?, request, response, redacted?}`. Bodies are verbatim. Only secrets are replaced with `"<redacted>"` (token and cookie values, the webhook signature); request headers keep only `content-type` and the NAMES of the cookies sent (`cookies_sent`). All names, emails, the bank account (`0000000000`) and bank code (`000`) are obvious placeholders. IDs and timestamps are the scratch database's own.

Where a step did not go over HTTP (job functions, reading a token from Redis, inserting the pending payment), the capture's `note` says so.

## Captures

| File | Call | Status | What it shows |
|---|---|---|---|
| `01-platform-admin-login.json` | `POST /api/v1/platform/auth/login` | 200 | Platform super admin signs in (cookies set; the body also carries the access token). |
| `02-platform-create-plain-admin.json` | `POST /api/v1/platform/auth/admins` | 201 | Super admin creates a plain ADMIN (used later to show the 403 on writes). |
| `03-platform-plain-admin-login.json` | `POST /api/v1/platform/auth/login` | 200 | The plain ADMIN signs in. |
| `04-admin-create-marketer.json` | `POST /api/v1/platform/referrals/` | 201 | Super admin onboards a marketer. Bank details go in; only bank_last4 ever comes back. Email delivery of the invite is not exercised live (SMTP points at an unused port). |
| `05-admin-create-marketer-duplicate-code.json` | `POST /api/v1/platform/referrals/` | 409 | Error: the referral code is already taken (different email, same code). |
| `06-admin-create-marketer-invalid-code.json` | `POST /api/v1/platform/referrals/` | 422 | Error: the code fails validation (too short and not a-z0-9-). |
| `07-admin-create-marketer-email-in-use.json` | `POST /api/v1/platform/referrals/` | 409 | Error: the email already belongs to a marketer (same email, different code). |
| `08-admin-list-marketers.json` | `GET /api/v1/platform/referrals/` | 200 | Admin lists marketers (any platform admin may read). |
| `09-admin-get-marketer.json` | `GET /api/v1/platform/referrals/8ea86181-0cfa-4149-a975-bd113db4265e` | 200 | Admin reads one marketer. |
| `10-admin-get-marketer-not-found.json` | `GET /api/v1/platform/referrals/11111111-1111-4111-8111-111111111111` | 404 | Error: unknown marketer id. |
| `11-marketer-login-before-password.json` | `POST /api/v1/auth/marketer/login` | 401 | Login before any password is set: 401, the same body as a wrong password. |
| `12-marketer-set-password-invalid-token.json` | `POST /api/v1/auth/marketer/set-password` | 400 | Error: an invalid or expired token. |
| `13-marketer-set-password.json` | `POST /api/v1/auth/marketer/set-password` | 200 | Marketer redeems the one-time invite token and sets a password (does not sign in). |
| `14-marketer-set-password-token-reused.json` | `POST /api/v1/auth/marketer/set-password` | 400 | Error: the same token cannot be used twice. |
| `15-marketer-login-wrong-password.json` | `POST /api/v1/auth/marketer/login` | 401 | Error: wrong password. |
| `16-marketer-login-unknown-email.json` | `POST /api/v1/auth/marketer/login` | 401 | Error: unknown email. Identical status and body to the wrong-password case. |
| `17-marketer-login.json` | `POST /api/v1/auth/marketer/login` | 200 | Marketer signs in: tenant-style HttpOnly cookies (marketer_access_token, marketer_refresh_token); the body is only {role, redirect}. |
| `18-admin-suspend-marketer.json` | `POST /api/v1/platform/referrals/8ea86181-0cfa-4149-a975-bd113db4265e/suspend` | 200 | Admin suspends the marketer (unpaid rewards move to held; there are none yet). |
| `19-marketer-me-while-suspended.json` | `GET /api/v1/marketer/me` | 401 | A session that was valid before the suspension stops working: 401. |
| `20-marketer-login-suspended.json` | `POST /api/v1/auth/marketer/login` | 403 | Login while suspended: 403 account_suspended (distinct from the 401 credential errors). |
| `21-admin-reactivate-marketer.json` | `POST /api/v1/platform/referrals/8ea86181-0cfa-4149-a975-bd113db4265e/reactivate` | 200 | Admin reactivates the marketer. |
| `22-marketer-login-after-reactivation.json` | `POST /api/v1/auth/marketer/login` | 200 | Login works again after reactivation. |
| `23-marketer-refresh.json` | `POST /api/v1/auth/marketer/refresh` | 200 | Rotate the refresh token (cookie only; the body is {token_type, expires_in}). |
| `24-marketer-logout.json` | `POST /api/v1/auth/marketer/logout` | 200 | Logout revokes the session server-side and clears the cookies. Always 200. |
| `25-marketer-me-after-logout.json` | `GET /api/v1/marketer/me` | 401 | The revoked access token no longer works: 401. |
| `26-marketer-forgot-password.json` | `POST /api/v1/auth/marketer/forgot-password` | 200 | Forgot password for a real marketer: generic 200 (the mail is sent after the response). |
| `27-marketer-forgot-password-unknown-email.json` | `POST /api/v1/auth/marketer/forgot-password` | 200 | Forgot password for an unknown email: the identical generic 200. |
| `28-marketer-reset-password.json` | `POST /api/v1/auth/marketer/set-password` | 200 | Reset: the same set-password endpoint redeems the reset token. |
| `29-marketer-login-old-password.json` | `POST /api/v1/auth/marketer/login` | 401 | Error: the old password no longer works. |
| `30-marketer-login-new-password.json` | `POST /api/v1/auth/marketer/login` | 200 | Login with the new password (this session is used for the rest of the run). |
| `31-marketer-me.json` | `GET /api/v1/marketer/me` | 200 | The marketer's profile: name, code and the shareable signup link. |
| `32-marketer-referrals-empty.json` | `GET /api/v1/marketer/referrals` | 200 | No referrals yet. |
| `33-marketer-earnings-empty.json` | `GET /api/v1/marketer/earnings` | 200 | No earnings yet. |
| `34-signup-with-referral-code.json` | `POST /api/v1/auth/register-company` | 201 | A company signs up with the marketer's code (form field). |
| `35-signup-with-unknown-code.json` | `POST /api/v1/auth/register-company` | 201 | A signup with an unknown code: the identical success shape (a bad code never fails or changes a signup). |
| `36-signup-company-name-taken.json` | `POST /api/v1/auth/register-company` | 400 | Error: the company name already exists (a plain 400 string detail). |
| `37-marketer-referrals-signed-up.json` | `GET /api/v1/marketer/referrals` | 200 | The referred company appears as signed_up, with a masked email. |
| `38-webhook-invalid-signature.json` | `POST /api/v1/webhooks/paystack` | 400 | Error: a webhook with a wrong signature is refused (400) and changes nothing. |
| `39-webhook-charge-success-first-payment.json` | `POST /api/v1/webhooks/paystack` | 200 | The first Paystack charge.success of the referred tenant: subscription upgraded and the referral reward created (nothing in the response says so; read it via the marketer API). |
| `40-webhook-charge-success-replay.json` | `POST /api/v1/webhooks/paystack` | 200 | Paystack redelivers the same event: already_processed, no second reward. |
| `41-marketer-referrals-converted.json` | `GET /api/v1/marketer/referrals` | 200 | The referral is now converted. |
| `42-marketer-earnings-pending.json` | `GET /api/v1/marketer/earnings` | 200 | Earnings after conversion: the reward is pending (inside the hold period). |
| `43-signup-rate-limited.json` | `POST /api/v1/auth/register-company` | 429 | Error: the per-IP signup budget (3 registrations per hour) is exhausted: 429. |
| `44-signup-referred-tenant-b.json` | `POST /api/v1/auth/register-company` | 201 | Another referred company (Placeholder Traders Ltd) signs up with the same code. |
| `45-signup-referred-tenant-c.json` | `POST /api/v1/auth/register-company` | 201 | Another referred company (Placeholder Mart Ltd) signs up with the same code. |
| `46-signup-self-referral-same-email-domain.json` | `POST /api/v1/auth/register-company` | 201 | A signup whose admin email shares the marketer's email domain: the same 201 shape, but it is treated as a self-referral (never rewarded, hidden from the marketer's list). |
| `47-webhook-charge-success-tenant-b.json` | `POST /api/v1/webhooks/paystack` | 200 | First payment of the referred tenant B, which shares a website domain with another referred tenant: the reward is created held. |
| `48-webhook-charge-success-tenant-c.json` | `POST /api/v1/webhooks/paystack` | 200 | First payment of the referred tenant C, which shares a website domain with another referred tenant: the reward is created held. |
| `49-admin-rewards-held-queue.json` | `GET /api/v1/platform/referrals/rewards` | 200 | The review queue (status=held): reasons and the other tenants sharing the domain. |
| `50-admin-rewards-all.json` | `GET /api/v1/platform/referrals/rewards` | 200 | The same queue with status=all (every status). |
| `51-admin-release-reward-note-missing.json` | `POST /api/v1/platform/referrals/rewards/44c54f25-9d4c-41f3-b344-376e4c332566/release` | 422 | Error: a note is required to release a reward. |
| `52-admin-release-reward-plain-admin.json` | `POST /api/v1/platform/referrals/rewards/44c54f25-9d4c-41f3-b344-376e4c332566/release` | 403 | Role matrix: a plain ADMIN cannot release (403). |
| `53-admin-release-reward.json` | `POST /api/v1/platform/referrals/rewards/44c54f25-9d4c-41f3-b344-376e4c332566/release` | 200 | Super admin releases a held reward after review. |
| `54-admin-release-reward-again.json` | `POST /api/v1/platform/referrals/rewards/44c54f25-9d4c-41f3-b344-376e4c332566/release` | 409 | Error: releasing a reward that is no longer held (409 reward_state_invalid). |
| `55-admin-void-reward.json` | `POST /api/v1/platform/referrals/rewards/f3cae87a-7a32-4883-ba46-875b344b8afb/void` | 200 | Super admin voids the other held reward. |
| `56-admin-release-reward-not-found.json` | `POST /api/v1/platform/referrals/rewards/11111111-1111-4111-8111-111111111111/release` | 404 | Error: unknown reward id. |
| `57-marketer-earnings-payable.json` | `GET /api/v1/marketer/earnings` | 200 | Earnings after the 7-day hold has passed: the reward is payable. |
| `58-admin-batches-list.json` | `GET /api/v1/platform/referrals/batches` | 200 | Payout batches, newest first. |
| `59-admin-batch-detail-draft.json` | `GET /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16` | 200 | One draft batch with its items (bank_last4 only) and current_amount_kobo per item. |
| `60-admin-batch-approve-plain-admin.json` | `POST /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16/approve` | 403 | Role matrix: a plain ADMIN cannot approve (403). |
| `61-admin-batch-approve.json` | `POST /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16/approve` | 200 | Super admin approves the batch. Default (manual) provider: items become awaiting_manual and nothing is sent anywhere. |
| `62-admin-batch-approve-again.json` | `POST /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16/approve` | 200 | Approving again is idempotent: 200 with the batch unchanged. |
| `63-admin-batch-mark-paid-wrong-amount.json` | `POST /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16/mark-paid` | 409 | Error: the amount the admin says they paid differs from the item's current amount (409 amount_mismatch, with current_amount_kobo). Nothing changes. |
| `64-admin-batch-mark-paid.json` | `POST /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16/mark-paid` | 200 | Admin records the manual bank transfer with the correct amount: item and batch paid. |
| `65-admin-batch-mark-paid-again.json` | `POST /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16/mark-paid` | 409 | Error: marking the same batch paid again (409, the batch is no longer open). |
| `66-admin-batch-detail-paid.json` | `GET /api/v1/platform/referrals/batches/1abf244b-1478-4866-a8e8-5c71033dac16` | 200 | The batch after payment. |
| `67-admin-batch-not-found.json` | `GET /api/v1/platform/referrals/batches/11111111-1111-4111-8111-111111111111` | 404 | Error: unknown batch id. |
| `68-marketer-earnings-after-payout.json` | `GET /api/v1/marketer/earnings` | 200 | Earnings after the payout: paid_total and the payouts history list. |
| `69-marketer-referrals-final.json` | `GET /api/v1/marketer/referrals` | 200 | Referrals at the end of the run: three referred companies; the unknown-code and self-referral signups are not listed. |
| `70-admin-config-get.json` | `GET /api/v1/platform/referrals/config` | 200 | Effective programme settings and where each value comes from (env or admin override). |
| `71-admin-config-patch-out-of-bounds.json` | `PATCH /api/v1/platform/referrals/config` | 422 | Error: a value outside the allowed bounds (422). |
| `72-admin-config-patch.json` | `PATCH /api/v1/platform/referrals/config` | 200 | Super admin changes the hold period (affects rewards created afterwards only). |
| `73-role-plain-admin-create-marketer.json` | `POST /api/v1/platform/referrals/` | 403 | Role matrix: a plain ADMIN cannot create a marketer (403). |
| `74-role-plain-admin-list-marketers.json` | `GET /api/v1/platform/referrals/` | 200 | Role matrix: a plain ADMIN can read. |
| `75-role-no-auth-platform.json` | `GET /api/v1/platform/referrals/` | 401 | Role matrix: no credentials on a platform route (401). |
| `76-role-no-auth-marketer-me.json` | `GET /api/v1/marketer/me` | 401 | Role matrix: no credentials on a marketer route (401). |
| `77-role-tenant-token-on-marketer-me.json` | `GET /api/v1/marketer/me` | 401 | Role matrix: a tenant admin's token on /marketer/me (401). |
| `78-role-marketer-token-on-platform-route.json` | `GET /api/v1/platform/referrals/` | 401 | Role matrix: a marketer's token on a platform route (401). |

## How the app was run

A scratch Postgres database (`ref_capture`, migrated with `alembic upgrade head`) and Redis logical
database 5; uvicorn on 127.0.0.1:18765 started by hand with a throwaway Fernet `ENCRYPTION_KEY` and
`ENVIRONMENT=staging` (the app's relaxed per-IP rate limits for auth endpoints; the signup limit of
3 per hour per IP is the same in every environment, so the script clears that one Redis counter once,
right after capturing the 429). SMTP pointed at an unused local port: every send fails fast and is
logged, no email leaves the machine. Qdrant and OpenAI were never needed by these flows.

## Easy to miss (all visible in the captures)

- Cookie names for the marketer session are `marketer_access_token` (path `/`) and
  `marketer_refresh_token` (path `/api/v1/auth/marketer`); both are `HttpOnly; Secure; SameSite=lax`.
  Login returns only `{"role": "marketer", "redirect": "/marketer"}`.
- Error bodies come in three shapes: `{"detail": {"code", "message"}}` (domain errors),
  `{"detail": "<string>"}` (auth failures, the signup 400) and the FastAPI 422 list. The webhook uses
  `{"error": "..."}`. The 429 is `{"detail": {"error", "limit", "window_seconds",
  "retry_after_seconds"}}`.
- A signup whose admin email shares the marketer's (non-webmail) email domain is a self-referral:
  same 201, never listed for the marketer, never rewarded. The captures use `@example.org` for the
  marketer for that reason.
- The marketer's `/referrals` shows a referral whose reward was **voided** as `signed_up` (with
  `trial_days_left`), even though the tenant paid.
- Rewards created from signups that share one IP and user agent also carry the `shared_origin`
  reason, besides `shared_domain`.
- `GET /marketer/me` `link` is built from `REFERRAL_LINK_BASE_URL`, not from `FRONTEND_URL`.
- `PATCH /config` returns `source_by_key` (`env` or `db`); the changed value only affects rewards
  created afterwards.

## Not exercised live

These were NOT run against anything real, and the captures say nothing about them. Anything the
frontend guide states about them has to be labelled as coming from code or docs, not from a capture.

- **Paystack Transfers provider against the real API**: recipient creation, `initiate_transfer`,
  `verify_transfer` and the `transfer.success` / `transfer.failed` / `transfer.reversed` webhooks.
  The payout was exercised with the default **manual** provider only (items become
  `awaiting_manual`, an admin records the bank transfer with `mark-paid`). No real or test
  Paystack API call was made at all.
- **OTP-required transfers** (a Paystack account that requires an OTP to finalise API transfers).
- **The Paystack dispute webhook** (`charge.dispute.create`) payload shape: it is read defensively
  from public docs and was never captured from Paystack. A release/void refused with 409
  `reward_in_flight` (a held reward still tied to an in-flight transfer) needs a Paystack transfer
  in flight, so it is not reproduced either.
- **Email delivery** of all four marketer emails (invite, password reset, conversion notice and
  payout notice). SMTP pointed at an unused local port; sends fail fast and are logged. The
  one-time invite and reset tokens were read out of Redis instead of from an email link.
- **The real checkout** (`POST /api/v1/subscription/upgrade/initialize`): it calls the Paystack
  API. The pending `PaymentTransaction` rows were inserted directly and the `charge.success`
  payload is shaped from what the handler reads, not a captured Paystack delivery.
- **The `luran_ref` cookie and the frontend**: capturing the referral code from a link, the cookie
  itself and the signup form are frontend work. These captures send `referral_code` (and
  `referral_source`) in the signup body only.
- **A tenant login**: the tenant token used for the 401 sample was minted with the project's token
  function, because a real tenant login needs the temporary password that is only emailed.
- **The scheduled jobs themselves** (arq cron): the promote and prepare-batch *functions* were
  called once from the capture script with a shifted `now`; the cron registration was not run.
- **Browser behaviour** (CORS, Secure cookies in a real browser): the cookies carry `Secure`, so
  the script keeps its own cookie jar and sends the `Cookie` header explicitly over plain http.
