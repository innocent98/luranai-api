# FE integration guide: referral program (marketer recruits)

Backend: branch `feature/referral-backend` (PR to `develop`: `<fill when opened>`). SOP: `docs/sop/referral_program_backend.md`. Design: `docs/superpowers/specs/2026-10-28-referral-program-design.md`.

**Every JSON body below is copied from `docs/fe-integration-captures/referral/`** (78 files, one per call, see its `INDEX.md`), recorded by `scripts/capture_referral.py` against a locally running API on a scratch Postgres 15 and Redis 7. Each body is followed by the capture it came from. Where a long object was trimmed, the line under the block says what was cut; no field was ever renamed, added or tidied. Anything that could NOT be exercised live is marked **NOT CAPTURED** or **NOT LIVE**, and says where its shape came from (code, tests or public docs). Section 14 ends with a table of what is verified and how much to trust each part.

Placeholders: every name, email, bank code (`000`) and account number (`0000000000`) in the captures is an obvious fake. Ids and timestamps are the scratch database's own throwaway values. Token and cookie values are `<redacted>` in the captures, exactly as they appear below.

**Scope.** This PR is backend only. The marketer page, the admin screens, the `luran_ref` cookie script, the login-page fallback and the privacy-policy text are frontend work that this guide specifies; none of it exists yet.

## 1. What the feature is, in the FE's terms

Luran recruits marketers. A marketer gets a personal link (`https://luranai.com/register?ref=<code>`). When a company signs up through it and later makes its **first payment**, the marketer earns a **one-time commission** (default 5,000 naira, `500000` kobo). Nothing is earned from later payments, and nothing is clawed back (Luran gives no refunds).

There are three surfaces to build:

| Surface | Who uses it | What it does |
|---|---|---|
| **Signup page** | a visitor | keeps the referral code from the link in a `luran_ref` cookie and sends it as `referral_code` when registering (section 4) |
| **Marketer page** `/marketer` | a marketer (not a tenant user, not an admin) | shows link, code, referred companies, earnings and payout history (sections 5, 6) |
| **Admin screens** (platform admin area) | Luran staff | onboard marketers, review held rewards, approve and record payout batches, change settings (sections 7 to 9) |

Marketers are created by a **super admin only**; nobody can self-enroll. A marketer is a separate identity from tenant users and platform admins: own table, own token type, own cookies (section 2).

**Money is integer kobo everywhere** (`*_kobo` fields). Divide by 100 to show naira: `500000` is 5,000.00 naira. Never send or expect decimals; `mark-paid` and `PATCH /config` reject non-integers.

## 2. Who can call what (identities, cookies, roles)

| Identity | Sign-in endpoint | Cookies (all `HttpOnly; Secure; SameSite=lax`) | Used on |
|---|---|---|---|
| Marketer | `POST /api/v1/auth/marketer/login` | `marketer_access_token` (path `/`, 900 s), `marketer_refresh_token` (path `/api/v1/auth/marketer`, 604800 s) | `/api/v1/marketer/*` only |
| Platform admin (`ADMIN` or `SUPER_ADMIN`) | `POST /api/v1/platform/auth/login` | `access_token` (path `/`, 900 s), `refresh_token` (path `/api/v1/platform/auth/refresh`, 604800 s) | `/api/v1/platform/*` |
| Tenant user | existing tenant login | existing tenant cookies | unchanged; not used by this feature |

Marketer login, from `17-marketer-login.json` (the tokens are never in the body; only the cookies carry them):

```text
marketer_access_token=<redacted>; HttpOnly; Max-Age=900; Path=/; SameSite=lax; Secure
marketer_refresh_token=<redacted>; HttpOnly; Max-Age=604800; Path=/api/v1/auth/marketer; SameSite=lax; Secure
```
_(Set-Cookie headers of `17-marketer-login.json`)_

Platform admin login, from `01-platform-admin-login.json` (the body here also carries `access_token`, redacted in the capture):

```text
access_token=<redacted>; HttpOnly; Max-Age=900; Path=/; SameSite=lax; Secure
refresh_token=<redacted>; HttpOnly; Max-Age=604800; Path=/api/v1/platform/auth/refresh; SameSite=lax; Secure
```
_(Set-Cookie headers of `01-platform-admin-login.json`)_

**Marketer endpoints accept the marketer cookie only.** No `Authorization: Bearer`, no shared `access_token` cookie. Send requests with `credentials: "include"` from the same site. A tenant token, a platform token, no credentials, and a marketer token on a platform route are all a plain **401** (captures 75 to 78, section 10). Never forward marketer cookies to tenant or admin endpoints, or the reverse.

**Refresh-cookie path trap.** The marketer refresh cookie is scoped to the prefix `/api/v1/auth/marketer` (not just `/refresh`), so it is also sent on login, set-password, forgot-password and logout. That is deliberate: it lets logout revoke the refresh token server-side. The platform admin refresh cookie is scoped to `/api/v1/platform/auth/refresh` only.

**Role matrix on `/api/v1/platform/referrals/*`.**

| Action | `ADMIN` | `SUPER_ADMIN` | no / wrong credentials |
|---|---|---|---|
| Read (list marketers, get marketer, rewards queue, batches, config) | 200 | 200 | 401 |
| Write (create marketer, bank update, suspend, reactivate, release, void, approve, retry, mark-paid, prepare batch, PATCH config) | **403** | 200 | 401 |

The 403 body is a plain string, see section 10. Hide write buttons for plain admins (the dashboard already knows the admin's role); still handle the 403.

## 3. Error shapes (read this before writing any error handler)

Errors do NOT share one shape. Handle each by status and shape:

| Shape | Where it occurs | Example capture |
|---|---|---|
| `{"detail": {"code": "...", "message": "..."}}` | domain errors: marketer/reward/batch 404 and 409, marketer login 401/403, set-password 400 | `05`, `10`, `15`, `20`, `54`, `63` |
| `{"detail": "<string>"}` | auth failures (401 `Could not validate credentials`, 403 `Not enough permissions. Super admin access required.`) and the signup 400 | `19`, `52`, `36` |
| `{"detail": [ {type, loc, msg, input, ctx} ]}` | FastAPI validation 422 | `06`, `51`, `71` |
| `{"detail": {"error", "limit", "window_seconds", "retry_after_seconds"}}` | 429 on signup | `43` |
| `{"error": "<string>"}` | the Paystack webhook (not called by the FE) | `38` |

Rules of thumb: switch on `detail.code` when `detail` is an object, show `detail` when it is a string, and map the 422 list by `loc`. The 422 list echoes the submitted `input` (including a wrong account number); do not log or display it.

## 4. Signup page: the `luran_ref` cookie and `referral_code`

**NOT BUILT, FE work.** The backend never sets the cookie. The contract the frontend must implement (from the design, section 5.2; no backend capture can prove cookie behaviour):

| Rule | Value |
|---|---|
| Name | `luran_ref` |
| Value | the public referral code only (lowercase letters, digits, hyphen). Nothing else, no tenant data |
| Lifetime | 30 days (`REFERRAL_COOKIE_DAYS` default 30; the backend setting is informational, the script owns the cookie) |
| Scope | first-party on `luranai.com` (the marketing site and `/register` share the host); path `/` recommended |
| Set when | any `luranai.com` page is opened with `?ref=<code>` |
| First link wins | if an unexpired `luran_ref` exists, a later different `?ref=` does NOT overwrite it |
| Used by | the signup form reads it and sends it as `referral_code` with `referral_source: "cookie"` |
| Fallback | a visible optional "Referral code" field on the form, pre-filled from the cookie; send `referral_source: "form"` when the visitor typed or edited it |

`POST /api/v1/auth/register-company` takes two new optional fields, `referral_code` and `referral_source` (`"cookie"` or `"form"`). Request and success response, from `34-signup-with-referral-code.json`:

```json
{
  "company_name": "Placeholder Stores Ltd",
  "admin_full_name": "Tola Placeholder",
  "admin_email": "tola.placeholder@example.com",
  "referral_code": "ada-placeholder",
  "referral_source": "form"
}
```
_(request body of `34-signup-with-referral-code.json`)_

```json
{
  "message": "Company registered successfully. Please check your email to verify your account.",
  "tenant_id": "32f02ca4-a6b0-4391-928e-337b1387e7e4",
  "admin_email": "tola.placeholder@example.com",
  "verification_required": true
}
```
_(response body of `34-signup-with-referral-code.json`, status 201)_

**A bad code never fails or changes a signup.** An unknown code gives the identical 201 shape (compare `35-signup-with-unknown-code.json`: same four keys, only ids and emails differ):

```json
{
  "message": "Company registered successfully. Please check your email to verify your account.",
  "tenant_id": "d9b16207-04a0-41ad-950a-5b520c047cb0",
  "admin_email": "uche.placeholder@example.com",
  "verification_required": true
}
```
_(response body of `35-signup-with-unknown-code.json`, status 201)_

From code and tests, **not captured**: a non-string, blank or over-64-character `referral_code` is treated as "no code" instead of a 422, and any `referral_source` other than `cookie`/`form` is **dropped** (not rejected): the attribution then proceeds as `form` (when the code itself is usable); a code is matched case-insensitively and trimmed; a suspended marketer's code is ignored the same way. The form must not tell the visitor whether the code was accepted: the response is deliberately identical.

The signup 400 for a taken company name is a plain string (`36-signup-company-name-taken.json`, status 400):

```json
{
  "detail": "Company with name 'Placeholder Stores Ltd' already exists"
}
```
_(response body of `36-signup-company-name-taken.json`, status 400)_

The signup is limited to 3 registrations per hour per IP, in every environment. The 429 (`43-signup-rate-limited.json`, status 429):

```json
{
  "detail": {
    "error": "Rate limit exceeded",
    "limit": 3,
    "window_seconds": 3600,
    "retry_after_seconds": 3600
  }
}
```
_(response body of `43-signup-rate-limited.json`, status 429)_

**Self-referral is silent.** A signup whose admin email equals the marketer's email, or shares the marketer's email domain (webmail domains such as gmail, yahoo, outlook, hotmail and icloud are exempt), still returns the normal 201 (`46-signup-self-referral-same-email-domain.json`, same shape as above), but the attribution is marked self-referral: it is hidden from the marketer's list and totals and is never rewarded. Do not promise a marketer that every signup via their link will count. (The captures use `@example.org` for the marketer for exactly this reason: using the same domain as the test tenants made the list come back empty.)

**Privacy policy.** The cookie and the commission programme must be disclosed in the privacy policy before this goes live (owner action, see the SOP).

## 5. Marketer authentication

All bodies are under `/api/v1/auth/marketer`.

**Login page flow (FE work).** Marketers use the normal login page. The page first calls the existing tenant login. If that fails with its credential error, call `POST /api/v1/auth/marketer/login` with the same email and password. On 200 go to the `redirect` from the body (`/marketer`). Two separate endpoints, two separate cookie sets: a browser holding a tenant or admin session is never treated as a marketer one. If the marketer login also fails, show the generic credential error. Show the suspension message only for the marketer **403**.

`POST /login`, request (`17-marketer-login.json`) and response:

```json
{
  "email": "ada.placeholder@example.org",
  "password": "Placeholder-Pass-123!"
}
```
_(request body of `17-marketer-login.json`)_

```json
{
  "role": "marketer",
  "redirect": "/marketer"
}
```
_(response body of `17-marketer-login.json`, status 200)_

Login errors. Unknown email, wrong password and "invite not yet accepted (no password set)" are **the same 401** with the same body, so do not try to tell them apart (`11`, `15`, `16`):

```json
{
  "detail": {
    "code": "invalid_credentials",
    "message": "Incorrect email or password"
  }
}
```
_(response body of `15-marketer-login-wrong-password.json`, status 401)_

Suspended marketer with the correct password gets a **403** (`20-marketer-login-suspended.json`), the only place to show the suspension message:

```json
{
  "detail": {
    "code": "account_suspended",
    "message": "This account has been suspended. Contact Luran support."
  }
}
```
_(response body of `20-marketer-login-suspended.json`, status 403)_

An invalid email syntax is a 422 from validation rather than the 401 (from the tests, **NOT CAPTURED**). The login, forgot-password and set-password endpoints share one rate-limit budget of 10 calls per 15 minutes per IP in production (100 in staging and development); the 429 body for it was **NOT CAPTURED**, handle any 429 by status and the `Retry-After` header.

**Session lifetime and refresh.** The access cookie lives 900 s. When a `/marketer/*` call returns 401 (`Could not validate credentials`), call `POST /refresh` once with no body (the refresh cookie rides along), then retry the original call once. If the refresh fails, go to login.

`POST /refresh` (`23-marketer-refresh.json`, no request body; both cookies are rotated):

```json
{
  "token_type": "bearer",
  "expires_in": 900
}
```
_(response body of `23-marketer-refresh.json`, status 200)_

A failed refresh is a 401 with `{"detail": {"code": "invalid_refresh_token", "message": "Invalid or expired refresh token"}}` (or `"No refresh token"` when the cookie is absent) and **no Set-Cookie**, so a tab that lost a refresh race does not wipe the winner's cookies. **NOT CAPTURED**, from code.

`POST /logout` always answers 200 and clears both cookies, even with no or an invalid session (`24-marketer-logout.json`):

```json
{
  "message": "Logged out"
}
```
_(response body of `24-marketer-logout.json`, status 200)_

After logout the old access token is dead (`25-marketer-me-after-logout.json`, status 401):

```json
{
  "detail": "Could not validate credentials"
}
```
_(response body of `25-marketer-me-after-logout.json`, status 401)_

**Suspension.** An admin suspending a marketer kills that marketer's live session on its next call (401 on `/marketer/me`, `19-marketer-me-while-suspended.json`) and blocks login with the 403 above. A refresh by a suspended marketer is also a 401.

**Invite, set-password and reset.** Onboarding emails a one-time link `{FRONTEND_URL}/marketer/set-password?token=<token>` (the email itself was **not exercised live**, section 13). The token is valid 7 days. The page posts it to `POST /set-password`:

```json
{
  "token": "<redacted>",
  "new_password": "Placeholder-Pass-123!"
}
```
_(request body of `13-marketer-set-password.json`)_

```json
{
  "message": "Password set. You can now sign in."
}
```
_(response body of `13-marketer-set-password.json`, status 200)_

`set-password` does **not** sign the marketer in; send them to login afterwards. The password must be 8 to 100 characters (422 otherwise, **NOT CAPTURED**, and the password is validated before the token is consumed). The same endpoint redeems a reset token (`28-marketer-reset-password.json`, same 200 body). An invalid, expired or already-used token (`12`, `14`) is a 400:

```json
{
  "detail": {
    "code": "invalid_or_expired_token",
    "message": "This link is invalid or has expired."
  }
}
```
_(response body of `14-marketer-set-password-token-reused.json`, status 400)_

Tokens are single-use. A successful set-password retires every other outstanding invite and reset link of that marketer.

**Invites cannot be re-sent.** There is no resend endpoint. If the invite email never arrived or expired, the marketer uses **Forgot password** on the login page, which works even before a password was ever set. Surface this on the admin marketer screen next to `invite_pending: true`.

`POST /forgot-password` answers the **identical** generic 200 for a real and an unknown email (`26`, `27`); the mail is sent after the response. A reset link is valid 60 minutes. A second request for the same marketer within 120 seconds sends no second mail but still answers 200. Do not say "email sent" or "no such account"; show a neutral message.

```json
{
  "email": "ada.placeholder@example.org"
}
```
_(request body of `26-marketer-forgot-password.json`)_

```json
{
  "message": "If an active account exists for that email, a reset link has been sent."
}
```
_(response body of `26-marketer-forgot-password.json`, status 200)_

Griefing note: anyone who knows a marketer's email can trigger a reset email (rate limited, 120 s per-marketer cooldown, the reset does not invalidate the invite or the password). Nothing the FE can do; do not reveal whether the account exists.

## 6. Marketer page: `/api/v1/marketer/*`

Cookie auth only (section 2). All three are read-only GETs. Each marketer has a budget of 60 requests per minute across the three; a 429 shape was **NOT CAPTURED**. Fetch on page load and on window focus. There is no push channel and no need to poll faster than that.

### 6.1 `GET /me`

Name, code and the shareable link (`31-marketer-me.json`, status 200):

```json
{
  "name": "Ada Placeholder",
  "code": "ada-placeholder",
  "link": "https://luranai.com/register?ref=ada-placeholder"
}
```
_(response body of `31-marketer-me.json`, status 200)_

The link base comes from the `REFERRAL_LINK_BASE_URL` setting (default `https://luranai.com`), not from the dashboard's own URL. Show the `link` as given; do not rebuild it.

### 6.2 `GET /referrals?limit=&offset=`

`limit` defaults to 50, maximum 100 (422 above), `offset` defaults to 0. Newest signup first.

Empty (`32-marketer-referrals-empty.json`):

```json
{
  "items": [],
  "totals": {
    "signed_up": 0,
    "converted": 0,
    "conversion_rate": 0.0
  }
}
```
_(response body of `32-marketer-referrals-empty.json`, status 200)_

A referral that has signed up and is still in trial (`37-marketer-referrals-signed-up.json`), after conversion (`41-marketer-referrals-converted.json`):

```json
{
  "items": [
    {
      "company_name": "Placeholder Stores Ltd",
      "masked_email": "t***@e***.com",
      "signed_up_at": "2026-10-29T11:05:59.849187Z",
      "status": "signed_up",
      "trial_days_left": 30,
      "kb_uploaded": false,
      "widget_installed": false,
      "converted_at": null
    }
  ],
  "totals": {
    "signed_up": 1,
    "converted": 0,
    "conversion_rate": 0.0
  }
}
```
_(response body of `37-marketer-referrals-signed-up.json`, status 200)_

```json
{
  "items": [
    {
      "company_name": "Placeholder Stores Ltd",
      "masked_email": "t***@e***.com",
      "signed_up_at": "2026-10-29T11:05:59.849187Z",
      "status": "converted",
      "trial_days_left": null,
      "kb_uploaded": false,
      "widget_installed": false,
      "converted_at": "2026-10-29T11:06:01.036080Z"
    }
  ],
  "totals": {
    "signed_up": 1,
    "converted": 1,
    "conversion_rate": 1.0
  }
}
```
_(response body of `41-marketer-referrals-converted.json`, status 200)_

Field rules:

| Field | Rule |
|---|---|
| `status` | `signed_up` (still in trial, commission window open, no reward yet), `converted` (a reward exists that is not void), `not_converted` (everything else: trial over unpaid, window closed, **a reward that was voided**, or a company that has already paid and upgraded but whose reward has not been created yet, see below). `not_converted` was **NOT CAPTURED live**; it is from code and tests |
| `trial_days_left` | integer (rounded up) only while `signed_up`; **`null`** otherwise (present, not absent) |
| `converted_at` | timestamp only when `converted`; **`null`** otherwise |
| `kb_uploaded`, `widget_installed` | activation hints, computed only for `signed_up` rows; always `false` for the others. A failure computing them degrades to `false`, it never errors the page |
| `masked_email` | first character of the local part plus `***`, `@`, first character of the first domain label plus `***`, then everything after that label's first dot: `t***@e***.com`. Anything malformed becomes `***` |
| `totals.signed_up` | **all** visible referred companies, not "currently in trial" (it includes converted ones) |
| `totals.conversion_rate` | `converted / signed_up` as a fraction rounded to 4 decimals (`0.6667`), `0.0` when there are none. Show as a percentage |

Self-referral attributions are not listed and not counted. Totals cover all pages, not just the page returned.

**`not_converted` is not the same as "never paid".** The status is decided in this order: a reward that is `void` gives `not_converted` (a marketer cannot tell void from never-converted); any other reward gives `converted` (held and pending included); with no reward, the company is `signed_up` only while its trial is still running and the commission window is open, otherwise `not_converted`. Two cases are worth knowing:

* A company that **has paid** is upgraded at that moment (`subscription_status` becomes `active`, the trial end is cleared), so it is no longer in trial. If its reward does not exist yet (the reward is created behind the payment and a failure there is repaired by the daily reconcile job, 03:00 UTC), it shows `not_converted` until the reward exists, then `converted`. Do not present that as a failed referral; it self-corrects within a day.
* A company with a **void** reward shows `not_converted` with `trial_days_left: null` and `converted_at: null`, whether or not its trial is still open (tests: `test_a_void_reward_shows_not_converted_even_while_the_trial_is_open`). `signed_up` next to a void reward can only appear if the company's upgrade itself failed (the tenant is still `trialing`); that is a tenant-billing problem, not a referral one.

**Observed in the live run, and why (capture 69).** In `69-marketer-referrals-final.json` the company whose reward an admin voided (`Placeholder Mart Ltd`, capture 55) is listed as `signed_up` with `trial_days_left: 30`:

```json
{
  "items": [
    {
      "company_name": "Placeholder Mart Ltd",
      "masked_email": "c***@e***.com",
      "signed_up_at": "2026-10-29T11:06:01.717405Z",
      "status": "signed_up",
      "trial_days_left": 30,
      "kb_uploaded": false,
      "widget_installed": false,
      "converted_at": null
    },
    {
      "company_name": "Placeholder Traders Ltd",
      "masked_email": "b***@e***.com",
      "signed_up_at": "2026-10-29T11:06:01.204904Z",
      "status": "converted",
      "trial_days_left": null,
      "kb_uploaded": false,
      "widget_installed": false,
      "converted_at": "2026-10-29T11:06:02.737031Z"
    },
    {
      "company_name": "Placeholder Stores Ltd",
      "masked_email": "t***@e***.com",
      "signed_up_at": "2026-10-29T11:05:59.849187Z",
      "status": "converted",
      "trial_days_left": null,
      "kb_uploaded": false,
      "widget_installed": false,
      "converted_at": "2026-10-29T11:06:01.036080Z"
    }
  ],
  "totals": {
    "signed_up": 3,
    "converted": 2,
    "conversion_rate": 0.6667
  }
}
```
_(response body of `69-marketer-referrals-final.json`, status 200)_

This capture is **not** the rule, it is an artifact of the scratch run: the capture script inserted that company's payment directly and the tenant's upgrade failed there (the real `upgrade_subscription` sets `subscription_status = active` and clears `trial_ends_at`, so a really upgraded tenant is never `signed_up`). With the rule above, the same company reads `not_converted` (`trial_days_left: null`). The capture is kept unedited. Do not read `signed_up` as "has not paid" in general, and do not build logic on `trial_days_left` for a company whose reward is void. A marketer cannot see held or flagged rewards at all (a held reward is shown as converted and its money as pending).

### 6.3 `GET /earnings`

Empty, after conversion (pending), after the hold passed (payable), after payout (`33`, `42`, `57`, `68`):

```json
{
  "this_month_earned_kobo": 0,
  "pending_kobo": 0,
  "payable_kobo": 0,
  "paid_total_kobo": 0,
  "payouts": []
}
```
_(response body of `33-marketer-earnings-empty.json`, status 200)_

```json
{
  "this_month_earned_kobo": 500000,
  "pending_kobo": 500000,
  "payable_kobo": 0,
  "paid_total_kobo": 0,
  "payouts": []
}
```
_(response body of `42-marketer-earnings-pending.json`, status 200)_

```json
{
  "this_month_earned_kobo": 1000000,
  "pending_kobo": 0,
  "payable_kobo": 1000000,
  "paid_total_kobo": 0,
  "payouts": []
}
```
_(response body of `57-marketer-earnings-payable.json`, status 200)_

```json
{
  "this_month_earned_kobo": 1000000,
  "pending_kobo": 0,
  "payable_kobo": 0,
  "paid_total_kobo": 1000000,
  "payouts": [
    {
      "date": "2026-10-29T11:06:03.012625Z",
      "amount_kobo": 1000000,
      "batch": "2026-11-06"
    }
  ]
}
```
_(response body of `68-marketer-earnings-after-payout.json`, status 200)_

| Field | Meaning |
|---|---|
| `this_month_earned_kobo` | rewards converted in the current calendar month in **Africa/Lagos** time, void excluded. It does **not** shrink when a payout happens (capture 68 still shows `1000000`) |
| `pending_kobo` | rewards inside the 7-day hold **plus held rewards**. The marketer is never told a reward is held or why |
| `payable_kobo` | cleared the hold, plus rewards already put in a payout batch (`batched`) |
| `paid_total_kobo` | transferred |
| `payouts[]` | newest first, at most 50: `date` is when the payment was recorded, `batch` is the batch's `period_label` (a date string like `2026-11-06`) |

The marketer sees only these four sums. They never see reward statuses, fraud reasons, the payout item status, the transfer reference or failure reasons.

## 7. Admin: marketers (`/api/v1/platform/referrals`)

The captures call the collection routes with a trailing slash (`/api/v1/platform/referrals/`); use the same. Platform admin cookie; reads any admin, writes super admin only.

### 7.1 Create a marketer (super admin)

Request and response (`04-admin-create-marketer.json`, status 201):

```json
{
  "name": "Ada Placeholder",
  "email": "ada.placeholder@example.org",
  "code": "ada-placeholder",
  "bank_code": "000",
  "account_number": "0000000000",
  "account_name": "Ada Placeholder"
}
```
_(request body of `04-admin-create-marketer.json`)_

```json
{
  "id": "8ea86181-0cfa-4149-a975-bd113db4265e",
  "name": "Ada Placeholder",
  "email": "ada.placeholder@example.org",
  "code": "ada-placeholder",
  "status": "active",
  "bank_last4": "0000",
  "invite_pending": true,
  "created_at": "2026-10-29T11:05:56.971257Z"
}
```
_(response body of `04-admin-create-marketer.json`, status 201)_

Rules: `code` is 3 to 32 characters of `a-z`, `0-9` and hyphen, stored lowercase, unique case-insensitively, reserved words blocked, and **immutable** (to change a code, suspend the marketer and issue a new one). `account_number` must be exactly 10 digits, `bank_code` 1 to 10 characters. The bank details are write-only: the response carries only `bank_last4`. **`invite_pending`** is `true` until the marketer sets a password. If the invite email fails to send, the call still returns 201 and the failure is only logged; there is no resend (section 5).

Errors:

| Case | Capture | Status | Body |
|---|---|---|---|
| code already used | `05` | 409 | `detail.code` `code_taken`, message `That referral code is already in use` |
| email already a marketer or a tenant user | `07` | 409 | `detail.code` `email_in_use`, message `Email already in use` |
| invalid code | `06` | 422 | FastAPI list |
| plain admin | `73` | 403 | plain string |

```json
{
  "detail": {
    "code": "code_taken",
    "message": "That referral code is already in use"
  }
}
```
_(response body of `05-admin-create-marketer-duplicate-code.json`, status 409)_

```json
{
  "detail": {
    "code": "email_in_use",
    "message": "Email already in use"
  }
}
```
_(response body of `07-admin-create-marketer-email-in-use.json`, status 409)_

The invalid-code 422 (`06-admin-create-marketer-invalid-code.json`). Note the message text is the validator's own and is generic for every kind of invalid code, and `input` echoes the submitted value:

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body",
        "code"
      ],
      "msg": "Value error, Referral code must be 3-32 characters long",
      "input": "A!",
      "ctx": {
        "error": {}
      }
    }
  ]
}
```
_(response body of `06-admin-create-marketer-invalid-code.json`, status 422)_

**Slow or failed mail.** The invite is emailed inside this request, after the marketer is saved. The SMTP socket timeout is **30 seconds** (it was 10; quote-PDF attachments share the same sender), so in the worst case the call takes about that long: keep the button disabled and the spinner up. A mail failure never fails the request; `invite_pending: true` is how you see that the marketer has not set a password yet.

**Writes are audited atomically.** Create, bank update, suspend and reactivate commit **together with their audit entry**. If the audit entry cannot be written, nothing is changed (no marketer, no new bank account, no status change, no invite) and the response is a `500` with a fixed body (**NOT CAPTURED**; from tests `test_bank_change_whose_audit_fails_is_rolled_back_with_a_fixed_500` and siblings):

```json
{"detail": {"code": "bank_update_failed", "message": "The change was not saved"}}
```

`code` is `marketer_create_failed`, `bank_update_failed`, `marketer_suspend_failed` or `marketer_reactivate_failed` depending on the call. Show "not saved, try again"; re-read the marketer before retrying.

### 7.2 List, get, suspend, reactivate

`GET /` takes `status` (`active` or `suspended`, **not captured**), `limit` (default 50, max 200) and `offset`.

```json
{
  "items": [
    {
      "id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "name": "Ada Placeholder",
      "email": "ada.placeholder@example.org",
      "code": "ada-placeholder",
      "status": "active",
      "bank_last4": "0000",
      "invite_pending": true,
      "created_at": "2026-10-29T11:05:56.971257Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```
_(response body of `08-admin-list-marketers.json`, status 200)_

`GET /{marketer_id}` (`09`, same item shape) and its 404 (`10-admin-get-marketer-not-found.json`):

```json
{
  "detail": {
    "code": "marketer_not_found",
    "message": "Marketer not found"
  }
}
```
_(response body of `10-admin-get-marketer-not-found.json`, status 404)_

`POST /{marketer_id}/suspend` and `/reactivate` take no body and return the marketer (`18`, `21`). Note what changed in `18-admin-suspend-marketer.json`: `status` is `suspended` and `invite_pending` is `false` (the password had been set by then):

```json
{
  "id": "8ea86181-0cfa-4149-a975-bd113db4265e",
  "name": "Ada Placeholder",
  "email": "ada.placeholder@example.org",
  "code": "ada-placeholder",
  "status": "suspended",
  "bank_last4": "0000",
  "invite_pending": false,
  "created_at": "2026-10-29T11:05:56.971257Z"
}
```
_(response body of `18-admin-suspend-marketer.json`, status 200)_

Suspending also moves that marketer's unpaid `pending` and `payable` rewards to `held` with the reason `marketer_suspended` (none existed in the capture; from code). **Reactivating does not release them**; an admin releases each from the review queue (section 8). Suspending stops new attribution to the code.

`PATCH /{marketer_id}/bank` (super admin) takes `{bank_code, account_number, account_name}` (all required, same rules as create, no other fields) and returns the same marketer shape. **NOT CAPTURED**; shape from the request model and the response model shown above. It clears the cached Paystack recipient so the next transfer goes to the new account.

## 8. Admin: reward review queue

`GET /api/v1/platform/referrals/rewards?status=&limit=&offset=`. `status` is one of `pending`, `held` (the default), `payable`, `batched`, `paid`, `void`, `all`. `limit` default 50, max 200. Newest conversion first. Any platform admin may read.

Held queue, trimmed to the first item (`49-admin-rewards-held-queue.json`, status 200):

```json
{
  "items": [
    {
      "id": "f3cae87a-7a32-4883-ba46-875b344b8afb",
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "marketer_name": "Ada Placeholder",
      "marketer_code": "ada-placeholder",
      "tenant_company_name": "Placeholder Mart Ltd",
      "amount_kobo": 500000,
      "status": "held",
      "review_reasons": [
        "shared_domain",
        "shared_origin"
      ],
      "converted_at": "2026-10-29T11:06:02.780559Z",
      "payable_after": "2026-11-05T11:06:02.780559Z",
      "payout_batch_id": null,
      "shared_domain_matches": [
        {
          "other_tenant_name": "Placeholder Stores Ltd",
          "domain": "example.com",
          "sources": [
            "widget"
          ],
          "own_sources": [
            "widget"
          ]
        },
        {
          "other_tenant_name": "Placeholder Traders Ltd",
          "domain": "example.com",
          "sources": [
            "widget"
          ],
          "own_sources": [
            "widget"
          ]
        }
      ]
    }
  ],
  "total": 2,
  "limit": 50,
  "offset": 0
}
```
_(response body of `49-admin-rewards-held-queue.json`, status 200; `items` trimmed to its first 1)_

Full status listing, trimmed to the third item, a `pending` reward with no flags (`50-admin-rewards-all.json`). Note `shared_domain_matches` is an empty list, not absent:

```json
{
  "items": [
    {
      "id": "989b87c9-9fc1-4d90-ba4c-d22b666ecc45",
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "marketer_name": "Ada Placeholder",
      "marketer_code": "ada-placeholder",
      "tenant_company_name": "Placeholder Stores Ltd",
      "amount_kobo": 500000,
      "status": "pending",
      "review_reasons": [],
      "converted_at": "2026-10-29T11:06:01.036080Z",
      "payable_after": "2026-11-05T11:06:01.036080Z",
      "payout_batch_id": null,
      "shared_domain_matches": []
    }
  ],
  "total": 3,
  "limit": 50,
  "offset": 0
}
```
_(response body of `50-admin-rewards-all.json`, status 200; `items` shows only element 2)_

| Field | Rule |
|---|---|
| `review_reasons` | list; empty for a clean reward. Captured: `shared_domain`, `shared_origin`. Others that exist in code (**NOT CAPTURED**): `shared_card`, `marketer_burst`, `marketer_suspended`, `payment_dispute`, `paid_in_flight`, `payment_reversed_after_hold`. Show unknown values as-is |
| `shared_domain_matches[]` | only for rewards with `shared_domain`: the other tenant's name, the registrable `domain`, and `sources` (where the *other* tenant's domain came from) and `own_sources` (this tenant's), values `audit`, `knowledge_url`, `widget`. `widget` and `knowledge_url` are weak evidence: a flag only holds a reward for a human, it never blocks anything. Discount weak evidence in the UI |
| `tenant_company_name` | `null` once the tenant has been purged |
| `payable_after` | `converted_at` plus the hold (7 days by default) |
| `payout_batch_id` | `null` unless the reward is in a batch |

`shared_origin` means referred signups from one marketer came from the same IP and user agent (a weak signal; both rewards in the capture carry it besides `shared_domain`). The fraud checks have honest limits: the same business under a different domain is not caught.

### Release and void (super admin)

`POST /rewards/{id}/release` and `/void` take `{"note": "<text>"}`: required, 1 to 1000 characters after trimming, kept in the audit entry only. **A blank or whitespace-only note is a 422** (`51`: the request sent `"   "`, the server echoes `input: ""`):

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": [
        "body",
        "note"
      ],
      "msg": "String should have at least 1 character",
      "input": "",
      "ctx": {
        "min_length": 1
      }
    }
  ]
}
```
_(response body of `51-admin-release-reward-note-missing.json`, status 422)_

Success returns only `{id, status}` (`53`, `55`). Release goes to `pending` when the reward's `payable_after` is still in the future (as captured) and to `payable` when it has passed; it is then paid in the next scheduled batch, never off-cycle. Void is final.

```json
{
  "note": "Checked the domain evidence: unrelated businesses."
}
```
_(request body of `53-admin-release-reward.json`)_

```json
{
  "id": "44c54f25-9d4c-41f3-b344-376e4c332566",
  "status": "pending"
}
```
_(response body of `53-admin-release-reward.json`, status 200)_

```json
{
  "id": "f3cae87a-7a32-4883-ba46-875b344b8afb",
  "status": "void"
}
```
_(response body of `55-admin-void-reward.json`, status 200)_

Errors: releasing a reward that is not `held` is `409 reward_state_invalid` (`54`); void works from `held`, `pending` and `payable` only; an unknown id is `404 reward_not_found` (`56`):

```json
{
  "detail": {
    "code": "reward_state_invalid",
    "message": "The reward is not in a state that allows this action"
  }
}
```
_(response body of `54-admin-release-reward-again.json`, status 409)_

```json
{
  "detail": {
    "code": "reward_not_found",
    "message": "Reward not found"
  }
}
```
_(response body of `56-admin-release-reward-not-found.json`, status 404)_

**`reward_in_flight` (409), NOT LIVE.** A held reward that is still linked to a payout item whose transfer may be live (processing or failed) cannot be released or voided yet. Shape from code and tests: `{"detail": {"code": "reward_in_flight", "message": "The reward is still tied to a payout batch; resolve that payout item first"}}`. Show the message and point the admin at the batch screen (retry the batch or record the payment first).

## 9. Admin: payout batches and settings

### 9.1 What happens on a schedule

* Daily 03:00 UTC: rewards whose hold has passed (and with no flag) become `payable`; missing rewards are repaired.
* The 1st and 15th, 04:00 UTC: a **draft** batch is prepared from every payable reward of every active marketer. **Nothing is paid until a super admin approves the batch.**
* If the 1st/15th job hits a database lock conflict (for example a marketer is being suspended at that very second) it retries up to 3 times with a short pause, then logs an error and gives up; the next scheduled run would be two weeks later, so a super admin can run it by hand (section 9.1A).
* The default payout provider is `manual`. With it, approving marks every item `awaiting_manual` and sends nothing anywhere; the admin pays by bank transfer outside Luran and records it with `mark-paid`. The automatic Paystack provider is switched on by configuration later (section 13).

**Switching the provider back to `manual`.** If the configuration was `paystack` and is set back to `manual`, items that were already sent through Paystack and are still `failed` or `processing` keep working: the server verifies them **with Paystack** (not with the manual provider) when you retry or mark them paid. A retry after Paystack confirms the transfer is dead claims a new attempt (`-r2`) and, because the setting is manual, parks it as `awaiting_manual` for a human to pay. The FE does nothing different; just do not assume that "provider is manual" means "no Paystack call can happen".

### 9.1A Prepare a batch by hand (super admin)

`POST /api/v1/platform/referrals/batches/prepare`, no body, super admin only (403 for an ordinary admin, 401 without a session), rate limited like the other admin routes. It does what the 1st/15th job does: groups every payable reward of every active marketer into a new **draft** batch. Nothing is sent; the batch still needs approval. **Added after the live capture run; verified by tests only (no capture).**

The response is always an object with a `batch` key: the full batch detail (same shape as `GET /batches/{id}`, section 9.2) or `null`:

```json
{"batch": null}
```

`{"batch": null}` with status 200 means nothing was payable (also what a second click returns, so the call is safe to repeat); otherwise `batch` is the draft (`status: "draft"`, every item `pending`). Failure is a `500` `{"detail": {"code": "prepare_failed", "message": "The batch was not prepared"}}`; try again. The audit entry (`prepare_payout_batch`) carries the batch id, status, total and item counts only.

### 9.2 Batches and items

`GET /batches?limit=&offset=` (any admin; newest first, `limit` max 200) and `GET /batches/{id}` (`58`, `59`, `66`, `67`). Batch list (`58-admin-batches-list.json`):

```json
{
  "items": [
    {
      "id": "1abf244b-1478-4866-a8e8-5c71033dac16",
      "period_label": "2026-11-06",
      "status": "draft",
      "total_kobo": 1000000,
      "item_counts": {
        "pending": 1,
        "processing": 0,
        "paid": 0,
        "failed": 0,
        "skipped": 0,
        "awaiting_manual": 0
      },
      "approved_at": null,
      "approved_by_admin_id": null,
      "created_at": "2026-10-29T11:06:02.908991Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```
_(response body of `58-admin-batches-list.json`, status 200)_

Batch detail while still a draft (`59-admin-batch-detail-draft.json`):

```json
{
  "id": "1abf244b-1478-4866-a8e8-5c71033dac16",
  "period_label": "2026-11-06",
  "status": "draft",
  "total_kobo": 1000000,
  "item_counts": {
    "pending": 1,
    "processing": 0,
    "paid": 0,
    "failed": 0,
    "skipped": 0,
    "awaiting_manual": 0
  },
  "approved_at": null,
  "approved_by_admin_id": null,
  "created_at": "2026-10-29T11:06:02.908991Z",
  "items": [
    {
      "id": "67b10382-335e-4931-9302-1b3115ddcafe",
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "marketer_name": "Ada Placeholder",
      "marketer_code": "ada-placeholder",
      "amount_kobo": 1000000,
      "current_amount_kobo": 1000000,
      "status": "pending",
      "failure_reason": null,
      "provider_status": null,
      "attempt": 1,
      "transfer_reference": null,
      "reward_count": 2,
      "reward_ids": [
        "44c54f25-9d4c-41f3-b344-376e4c332566",
        "989b87c9-9fc1-4d90-ba4c-d22b666ecc45"
      ],
      "bank_last4": "0000",
      "held_rewards_excluded": 0
    }
  ]
}
```
_(response body of `59-admin-batch-detail-draft.json`, status 200)_

`404` for an unknown batch (`67-admin-batch-not-found.json`):

```json
{
  "detail": {
    "code": "batch_not_found",
    "message": "Payout batch not found"
  }
}
```
_(response body of `67-admin-batch-not-found.json`, status 404)_

Field-nesting and value traps:

* `item_counts` always carries all six states, with zeros.
* `amount_kobo` is what was **claimed** when the item was approved (the amount the admin was shown and may already have paid at the bank). **`current_amount_kobo`** is what that marketer's rewards currently batched in this batch add up to. They differ when a reward was held (a payment dispute) after the item was claimed. `mark-paid` accepts either number for an `awaiting_manual` item (see 9.5), and `current_amount_kobo` becomes `0` once the item is paid (compare `64`/`66`).
* `held_rewards_excluded` is the marketer's held rewards that are not in this batch.
* `transfer_reference` is `null` in a draft, the system reference (`ref-<batch id>-<marketer id>`, with `-r2`, `-r3` after retries) once approved, and the **bank reference you typed** after a manual `mark-paid`.
* `failure_reason`, `provider_status`, `approved_at`, `approved_by_admin_id` are present as `null` when not set, never absent.

### 9.3 State machines

Reward status (the marketer sees only the sums of section 6.3):

| Status | Meaning | Leaves when |
|---|---|---|
| `pending` | first payment recorded, inside the hold | hold passes with no flag, becomes `payable`; a dispute or a marketer suspension makes it `held` |
| `held` | flagged or disputed, needs a human | admin release (to `pending` or `payable`) or void |
| `payable` | cleared the hold | next batch (becomes `batched`) |
| `batched` | in a payout batch | the item is paid (`paid`); a dispute holds it again |
| `paid` | transferred | final (a Paystack `transfer.reversed` puts it back to `batched`, or `held` if it was disputed or held in flight) |
| `void` | not payable | final |

Payout item status (admin only):

| Status | Meaning | What the UI offers |
|---|---|---|
| `pending` | prepared, not yet approved or claimed | nothing (approve the batch) |
| `processing` | handed to the provider, answer not final (Paystack transfers are asynchronous) | nothing; wait or poll. **Never** offer manual payment here, a live transfer could still succeed |
| `awaiting_manual` | manual provider: waiting for the admin to pay and record it. A reward disputed meanwhile **stays locked to this item** (it is `held` with its batch link, so release and void answer `409 reward_in_flight`): the admin may already have paid it at the bank | Mark paid |
| `paid` | done | nothing |
| `failed` | the provider failed, needed an OTP (`failure_reason` `otp_required`), or the transfer was reversed (`reversed`) | Retry the batch; or Mark paid, which the server only allows after it has confirmed the transfer is dead |
| `skipped` | marketer suspended, no batched rewards left, or a non-positive amount | nothing |

Batch status: `draft` (prepared, not approved), `paying` (approved; items in flight or waiting), `paid` (every non-skipped item paid), `failed` (nothing is in flight and some item failed, or every item was skipped). `approved` exists in the enum but is transient; the API answers `paying` straight away. A batch only ever becomes `paid` when money has moved.

**Polling.** Approve and retry run the provider calls **inside the request**, so the response already reflects most outcomes. Under the manual provider nothing is asynchronous: no polling. Under the Paystack provider an item can stay `processing` until Paystack's `transfer.success|failed|reversed` webhook arrives; suggested cadence (a suggestion, not measured): re-read `GET /batches/{id}` every 10 s for the first 2 minutes, then every 30 s while any item is `processing`, and stop when none is. An item stuck in `processing` beyond a few hours needs the manual runbook in the SOP; do not offer a button for it.

### 9.4 Approve (super admin)

No body. **While the request is pending, disable the button and show progress: it can take as long as one provider call per item.** Approve is idempotent: a second call on a batch that is no longer a draft returns 200 with the batch unchanged and sends nothing.

Approve with the manual provider (`61-admin-batch-approve.json`, status 200):

```json
{
  "id": "1abf244b-1478-4866-a8e8-5c71033dac16",
  "period_label": "2026-11-06",
  "status": "paying",
  "total_kobo": 1000000,
  "item_counts": {
    "pending": 0,
    "processing": 0,
    "paid": 0,
    "failed": 0,
    "skipped": 0,
    "awaiting_manual": 1
  },
  "approved_at": "2026-10-29T11:06:02.952893Z",
  "approved_by_admin_id": "494f0196-ca17-4ab5-b146-f502ca51291b",
  "created_at": "2026-10-29T11:06:02.908991Z",
  "items": [
    {
      "id": "67b10382-335e-4931-9302-1b3115ddcafe",
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "marketer_name": "Ada Placeholder",
      "marketer_code": "ada-placeholder",
      "amount_kobo": 1000000,
      "current_amount_kobo": 1000000,
      "status": "awaiting_manual",
      "failure_reason": null,
      "provider_status": "manual",
      "attempt": 1,
      "transfer_reference": "ref-1abf244b-1478-4866-a8e8-5c71033dac16-8ea86181-0cfa-4149-a975-bd113db4265e",
      "reward_count": 2,
      "reward_ids": [
        "44c54f25-9d4c-41f3-b344-376e4c332566",
        "989b87c9-9fc1-4d90-ba4c-d22b666ecc45"
      ],
      "bank_last4": "0000",
      "held_rewards_excluded": 0
    }
  ]
}
```
_(response body of `61-admin-batch-approve.json`, status 200)_

Approve again (`62-admin-batch-approve-again.json`, status 200) returns the identical body, including `approved_at`:

```json
{
  "id": "1abf244b-1478-4866-a8e8-5c71033dac16",
  "period_label": "2026-11-06",
  "status": "paying",
  "total_kobo": 1000000,
  "item_counts": {
    "pending": 0,
    "processing": 0,
    "paid": 0,
    "failed": 0,
    "skipped": 0,
    "awaiting_manual": 1
  },
  "approved_at": "2026-10-29T11:06:02.952893Z",
  "approved_by_admin_id": "494f0196-ca17-4ab5-b146-f502ca51291b",
  "created_at": "2026-10-29T11:06:02.908991Z",
  "items": [
    {
      "id": "67b10382-335e-4931-9302-1b3115ddcafe",
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "marketer_name": "Ada Placeholder",
      "marketer_code": "ada-placeholder",
      "amount_kobo": 1000000,
      "current_amount_kobo": 1000000,
      "status": "awaiting_manual",
      "failure_reason": null,
      "provider_status": "manual",
      "attempt": 1,
      "transfer_reference": "ref-1abf244b-1478-4866-a8e8-5c71033dac16-8ea86181-0cfa-4149-a975-bd113db4265e",
      "reward_count": 2,
      "reward_ids": [
        "44c54f25-9d4c-41f3-b344-376e4c332566",
        "989b87c9-9fc1-4d90-ba4c-d22b666ecc45"
      ],
      "bank_last4": "0000",
      "held_rewards_excluded": 0
    }
  ]
}
```
_(response body of `62-admin-batch-approve-again.json`, status 200)_

A plain admin gets the 403 plain string (`60`).

### 9.5 Retry (super admin) and mark-paid

`POST /batches/{id}/retry` (no body) re-drives the unfinished items of an approved batch: each item is looked up at Paystack first and only re-sent when no live transfer exists. It returns the batch detail shape above; a draft batch is a `409 batch_state_invalid`. **NOT CAPTURED** (it needs the Paystack provider).

`POST /batches/{id}/mark-paid` records payments made outside the system. The body lists one entry per marketer; all entries apply together or none do. **`amount_kobo` is required: the amount the admin actually paid, in kobo.** For an `awaiting_manual` item the server accepts **either** of two figures, and refuses everything else with `409 amount_mismatch`:

* the item's claimed `amount_kobo`: the admin paid what they were shown at approval. Every claimed reward is recorded as paid, including one that was disputed (held) after the claim (it gets the review reason `paid_in_flight`), so nothing is left that could be paid a second time;
* the item's `current_amount_kobo`: the admin paid only the undisputed part. That set is recorded and the disputed reward is released from the batch for an admin to handle in the review queue.

When no reward was held the two numbers are equal. For a `failed` item only `current_amount_kobo` is accepted. Show both numbers as naira when they differ, make the admin confirm which one they actually paid, and send that one back in kobo. **A reward disputed while its item is `awaiting_manual` cannot be released or voided until the item is marked paid** (`409 reward_in_flight`).

Wrong amount (`63-admin-batch-mark-paid-wrong-amount.json`): request, then the 409. Nothing changes. Refresh the batch, show the figures from the error, and ask the admin to check what they really paid. This capture predates the two-amount rule: its body lacks `claimed_amount_kobo` and carries the older message (kept verbatim below; the current body follows it):

```json
{
  "items": [
    {
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "bank_reference": "BANK-REF-PLACEHOLDER-001",
      "amount_kobo": 999999
    }
  ]
}
```
_(request body of `63-admin-batch-mark-paid-wrong-amount.json`)_

```json
{
  "detail": {
    "code": "amount_mismatch",
    "message": "The amount differs from what the item is worth now (a reward may have been held); check what you actually paid",
    "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
    "current_amount_kobo": 1000000
  }
}
```
_(response body of `63-admin-batch-mark-paid-wrong-amount.json`, status 409)_

Current body of the same refusal (changed after the live run; **verified by tests only**, test `test_r49_mark_paid_accepts_the_claimed_or_the_current_amount_after_a_reward_was_held`). `claimed_amount_kobo` is the item's claimed amount for an `awaiting_manual` item and **`null`** for a `failed` item (where only the current amount is accepted); the `message` is fixed text, the submitted amount is never echoed:

```json
{
  "detail": {
    "code": "amount_mismatch",
    "message": "The amount matches neither the item's current amount nor its claimed amount (a reward may have been held); check what you actually paid",
    "marketer_id": "<placeholder-marketer-uuid>",
    "current_amount_kobo": 500000,
    "claimed_amount_kobo": 800000
  }
}
```

Correct amount (`64-admin-batch-mark-paid.json`). Note `transfer_reference` is now the bank reference, `current_amount_kobo` is `0` and the batch is `paid`:

```json
{
  "items": [
    {
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "bank_reference": "BANK-REF-PLACEHOLDER-001",
      "amount_kobo": 1000000
    }
  ]
}
```
_(request body of `64-admin-batch-mark-paid.json`)_

```json
{
  "id": "1abf244b-1478-4866-a8e8-5c71033dac16",
  "period_label": "2026-11-06",
  "status": "paid",
  "total_kobo": 1000000,
  "item_counts": {
    "pending": 0,
    "processing": 0,
    "paid": 1,
    "failed": 0,
    "skipped": 0,
    "awaiting_manual": 0
  },
  "approved_at": "2026-10-29T11:06:02.952893Z",
  "approved_by_admin_id": "494f0196-ca17-4ab5-b146-f502ca51291b",
  "created_at": "2026-10-29T11:06:02.908991Z",
  "items": [
    {
      "id": "67b10382-335e-4931-9302-1b3115ddcafe",
      "marketer_id": "8ea86181-0cfa-4149-a975-bd113db4265e",
      "marketer_name": "Ada Placeholder",
      "marketer_code": "ada-placeholder",
      "amount_kobo": 1000000,
      "current_amount_kobo": 0,
      "status": "paid",
      "failure_reason": null,
      "provider_status": "manual",
      "attempt": 1,
      "transfer_reference": "BANK-REF-PLACEHOLDER-001",
      "reward_count": 2,
      "reward_ids": [
        "44c54f25-9d4c-41f3-b344-376e4c332566",
        "989b87c9-9fc1-4d90-ba4c-d22b666ecc45"
      ],
      "bank_last4": "0000",
      "held_rewards_excluded": 0
    }
  ]
}
```
_(response body of `64-admin-batch-mark-paid.json`, status 200)_

Marking a batch that is already closed is `409 batch_state_invalid` (`65-admin-batch-mark-paid-again.json`):

```json
{
  "detail": {
    "code": "batch_state_invalid",
    "message": "The batch is not in a state that allows this action"
  }
}
```
_(response body of `65-admin-batch-mark-paid-again.json`, status 409)_

Rules from code and tests (**NOT CAPTURED**): entries are `{marketer_id, bank_reference, amount_kobo}` (no other fields); `bank_reference` is trimmed, 1 to 100 characters, unique within the request and across payments; `amount_kobo` a strictly positive integer; an empty `items` list, duplicate marketers or a bool/float/string amount is a 422. Allowed only for `awaiting_manual` and `failed` items, never `pending`, `processing` or `paid`. Other 409 codes with fixed messages: `item_not_markable` (adds `marketer_id` and `item_status`), `transfer_unverified` (a `failed` item whose earlier transfer could not be confirmed dead), `bank_reference_in_use`, `payout_state_invalid`; `404 payout_item_not_found` when that marketer has no item in the batch.

After the payment is recorded the marketer's earnings show it (`68`) and, if the notification mail goes out, they get a payout email (not exercised live).

### 9.6 Settings: `GET` and `PATCH /config`

Any admin reads; a super admin changes. `source_by_key` says where each value comes from: `env` (the deployment default) or `db` (an admin override). Read (`70-admin-config-get.json`):

```json
{
  "reward_kobo": 500000,
  "hold_days": 7,
  "commission_window_days": 90,
  "source_by_key": {
    "reward_kobo": "env",
    "hold_days": "env",
    "commission_window_days": "env"
  }
}
```
_(response body of `70-admin-config-get.json`, status 200)_

`PATCH` takes any subset of the three keys (at least one; integers only, no nulls, no other keys). Bounds: `reward_kobo` 10000 to 5000000, `hold_days` 0 to 60, `commission_window_days` 1 to 365. Out of bounds is a 422 (`71`); success returns the full config (`72`):

```json
{
  "hold_days": 14
}
```
_(request body of `72-admin-config-patch.json`)_

```json
{
  "reward_kobo": 500000,
  "hold_days": 14,
  "commission_window_days": 90,
  "source_by_key": {
    "reward_kobo": "env",
    "hold_days": "db",
    "commission_window_days": "env"
  }
}
```
_(response body of `72-admin-config-patch.json`, status 200)_

```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": [
        "body",
        "reward_kobo"
      ],
      "msg": "Input should be greater than or equal to 10000",
      "input": 1,
      "ctx": {
        "ge": 10000
      }
    }
  ]
}
```
_(response body of `71-admin-config-patch-out-of-bounds.json`, status 422)_

**UX consequence.** A change affects rewards created **after** it (existing rewards keep their amount and hold date). The one exception is display: the marketer's `signed_up` / `not_converted` status uses the *current* commission window length measured from signup, so changing `commission_window_days` can flip what existing referrals show. Warn the admin when saving. The audit entry and the change commit together; a failure is a `500` with `{"detail": {"code": "config_update_failed", "message": "The settings were not changed"}}` (**NOT CAPTURED**; a very rare concurrent first override of the same key can also trip it, retry).

## 10. Role and isolation responses (captures 73 to 78, 52, 60, 19)

Plain admin write (`73-role-plain-admin-create-marketer.json`, status 403), plain admin read (`74`, 200), no credentials on a platform route (`75`, 401), no credentials on a marketer route (`76`, 401), tenant token on `/marketer/me` (`77`, 401), marketer token on a platform route (`78`, 401):

```json
{
  "detail": "Not enough permissions. Super admin access required."
}
```
_(response body of `73-role-plain-admin-create-marketer.json`, status 403)_

```json
{
  "detail": "Could not validate credentials"
}
```
_(response body of `75-role-no-auth-platform.json`, status 401)_

```json
{
  "detail": "Could not validate credentials"
}
```
_(response body of `78-role-marketer-token-on-platform-route.json`, status 401)_

The 401 string is the same everywhere (`Could not validate credentials`); the FE cannot tell "expired" from "wrong kind of token" and should not try to.

## 11. The Paystack webhook (not called by the FE)

Paystack calls `POST /api/v1/webhooks/paystack`. The FE never does. It matters to the FE only because the reward is created there, silently: `39-webhook-charge-success-first-payment.json` answers 200 and says nothing about a reward, and the marketer page changes only on its next fetch. A bad signature is `400 {"error": "Invalid signature"}` (`38`); a redelivery of the same event is `{"status": "success", "message": "Webhook processed", "result": {"status": "already_processed"}}` (`40`) and creates no second reward. The captured payloads are **shaped from what the handler reads, not captured from Paystack**. The dispute event (`charge.dispute.create`) and the transfer events (`transfer.success|failed|reversed`) were never exercised against real Paystack, section 13.

## 12. What the frontend MUST do (consolidated checklist)

These are consequences a user or admin would find surprising or wrong if shown silently. Each is an instruction.

**Signup page**
1. Capture `?ref=` into `luran_ref` (30 days, first link wins) and send it as `referral_code` with `referral_source`. Show the optional "Referral code" field pre-filled. Never show whether the code was accepted: the 201 is identical for known and unknown codes.
2. Do not promise a marketer that every signup counts. A signup using an email on the marketer's own (non-webmail) domain is a silent self-referral: same 201, never listed, never rewarded.
3. Add the `luran_ref` cookie and the commission programme to the privacy policy before launch.

**Login page**
4. Try the tenant login first, then `POST /api/v1/auth/marketer/login` on failure. Marketer cookies are `marketer_access_token` and `marketer_refresh_token` (refresh path `/api/v1/auth/marketer`). Redirect to `/marketer` on success.
5. Show "account suspended, contact support" only for the marketer 403 `account_suspended`. A suspended marketer's open session simply starts returning 401: send them to login.
6. Wrong password, unknown email and "never set a password" are the same 401; do not distinguish them.
7. Add "Forgot password" for marketers; it is also the only way to re-issue an invite, because invites cannot be re-sent. After set-password, send the user to login (it does not sign in).

**Marketer page**
8. All money is kobo; show naira with two decimals.
9. `not_converted` covers a voided reward and a paid company whose reward is not created yet (it turns `converted` after the daily reconcile); `totals.signed_up` counts every referred company. The page never shows why a reward is waiting: a held reward is just part of "pending".
10. `trial_days_left` and `converted_at` are `null` when not applicable; handle null, not absence.
11. `this_month_earned_kobo` is the Lagos calendar month and does not drop after a payout.

**Admin: marketers**
12. Creating a marketer needs super admin. Show `invite_pending` and warn that the invite cannot be re-sent. Bank details are write-only (`bank_last4` only). A code can never be changed.
13. Suspending a marketer holds their unpaid rewards (reason `marketer_suspended`); reactivating does **not** release them. Tell the admin to review the queue after reactivating.

**Admin: review queue**
14. Release and void need a non-blank note (the audit entry keeps it). Warn that void is final. Treat `reward_in_flight` 409 as "resolve the payout item first".
15. Show `review_reasons` and `shared_domain_matches` with their `sources`; `widget` and `knowledge_url` are weak evidence and a flag only ever holds a reward for a human.

**Admin: payouts**
16. **Approve processes the items inside the request**: disable the button while the call is pending and never fire it twice. Approving again is harmless (200, unchanged), but the first call can be slow.
17. **Mark paid requires `amount_kobo`.** Pre-fill nothing silently: show the item's `current_amount_kobo` and, when different, its claimed `amount_kobo` as naira, make the admin confirm which one they paid, and send it. On `409 amount_mismatch`, refresh the batch and show `current_amount_kobo` and `claimed_amount_kobo` from the error (a reward was held since the batch was approved).
18. Offer Mark paid only for `awaiting_manual` and `failed` items. Never for `processing` or `pending`: a live transfer could still succeed and pay twice. The server enforces this too.
19. Under the Paystack provider an item can stay `processing` for a while; poll as in section 9.3 and do not show it as failed.
20. Show `current_amount_kobo` as "what this marketer is owed now" and, for an `awaiting_manual` item where it differs from `amount_kobo`, explain that `amount_kobo` is what was approved and may already have been paid.
21. Warn before saving settings: a change affects rewards created afterwards; a new commission window also changes what existing referrals show to marketers.
22. There are no refunds and no clawback: a paid commission is kept, and a dispute after payment changes nothing. Do not build a "claw back" action.

## 12A. Field-nesting and present-but-null traps

* The same marketer shape (`id, name, email, code, status, bank_last4, invite_pending, created_at`) is returned by create, list items, get, suspend and reactivate. `bank_last4` is a string, `"0000"` in the captures.
* The marketer login/refresh/logout bodies carry **no token**; tokens are cookies only. The platform login body does carry `access_token`.
* Marketer-side `link` is built from `REFERRAL_LINK_BASE_URL`, not the dashboard host.
* `trial_days_left`, `converted_at`, `failure_reason`, `provider_status`, `transfer_reference`, `approved_at`, `approved_by_admin_id`, `payout_batch_id` are present and `null` when unset, never absent. `shared_domain_matches` is `[]` for a clean reward, never absent.
* `amount_kobo` versus `current_amount_kobo` on a batch item (section 9.2); `transfer_reference` changes meaning after a manual payment.
* Reward `release` and `void` return `{id, status}` only, not the full reward; re-read the queue.
* `payouts[].batch` is the batch's `period_label` string, not an id.
* The 401 string and the 403 string are plain `detail` strings, while every domain error is `detail.code` / `detail.message` (section 3).

## 13. Not exercised live (read before trusting a shape)

| Area | Why not | Where the shape in this guide comes from |
|---|---|---|
| Paystack Transfers provider against the real API: recipient creation, initiate, verify, `transfer.*` webhooks, OTP-required transfers | no real or test Paystack call was made; only the manual provider was run | code, 83 payout service tests with a mocked HTTP transport, and Paystack's public developer docs read by a prior task |
| Paystack dispute webhook payload (`charge.dispute.create`) and the `reward_in_flight` 409 | needs a live Paystack dispute / an in-flight transfer | event name confirmed from public docs; payload fields read defensively; tests use shaped payloads |
| Email delivery: invite, password reset, conversion notice, payout notice | SMTP pointed at an unused port; tokens were read from Redis | code, templates, 19 notification tests (with a local SMTP sink once, outside the committed captures) |
| Real checkout (`/subscription/upgrade/initialize`) | calls real Paystack | pending payment rows were inserted directly; the `charge.success` payload is shaped from the handler |
| `luran_ref` cookie, signup form, login-page fallback, marketer and admin UI | frontend work | this guide's section 4 and 5 |
| A real tenant login | needs an emailed temporary password | a tenant token minted with the project's token function (`77`) |
| The arq cron jobs themselves | only the functions were called, with a shifted clock (captures `57`, `58`) | cron registration is asserted in `tests/workers/test_referral_jobs.py` |
| Browser behaviour (CORS, `Secure` cookies) | the script sent cookies by hand over plain http | standard cookie semantics |
| `not_converted` status, 429 on marketer or admin routes, refresh failure, bank PATCH, batch retry, extra mark-paid error codes | no capture | code and tests, labelled inline |
| `POST /batches/prepare`, the two-amount `mark-paid` rule (claimed or current), the `claimed_amount_kobo` field of the 409, the `500` audit-failure bodies of the marketer writes, provider switched back to `manual` | added or changed after the live run | tests (`tests/referral/test_payout_endpoints.py`, `test_payouts.py`, `test_marketer_admin.py`), labelled inline |

## 14. Verification table

"Verified live" means a real HTTP request to the running app on a scratch database, with the response saved in the named capture. "Observed" means seen but suspicious.

| Behaviour | Status | Evidence |
|---|---|---|
| Admin creates marketer (201), bank last 4 only, `invite_pending: true` | verified live | `04`, `08`, `09` |
| Super admin creates a plain `ADMIN`, who signs in (used for the 403 rows below) | verified live | `01`, `02`, `03` |
| Duplicate code 409 `code_taken`; email in use 409 `email_in_use`; invalid code 422; unknown id 404 | verified live | `05`, `07`, `06`, `10` |
| Login before password set is the same 401 as wrong password and unknown email | verified live | `11`, `15`, `16` |
| Invite token set-password 200; reuse 400; garbage 400 | verified live | `13`, `14`, `12` |
| Marketer login sets `marketer_access_token` / `marketer_refresh_token`, body only `{role, redirect}`, refresh cookie path | verified live | `17`, `22`, `30` |
| Refresh rotates cookies; logout always 200 and kills the access token | verified live | `23`, `24`, `25` |
| Suspended: live session 401, login 403 `account_suspended`; reactivate restores login | verified live | `18` to `22` |
| Forgot password generic identical 200; reset via set-password; old password dead | verified live | `26`, `27`, `28`, `29`, `30` (reset token read from Redis; email not sent) |
| Refresh failure body, 422 for invalid login email, set-password password rule | NOT exercised live | code and tests, section 5 |
| Signup with code 201; unknown code identical 201; taken name 400 string; per-IP 429; further referred signups | verified live | `34`, `35`, `36`, `43`, `44`, `45` |
| Same-domain email signup is a silent self-referral (201, hidden, never rewarded) | verified live | `46`, absence from `69`, no reward in `50` |
| `referral_code` / `referral_source` coercion rules (over-long, blank, bad source) | NOT exercised live | tests, section 4 |
| `luran_ref` cookie, first-link-wins, form fallback | NOT built (FE) | design section 5.2 |
| First payment webhook creates a reward; replay creates none; bad signature 400 | verified live (payload shaped, not from Paystack) | `38`, `39`, `40`, `41`, `42` |
| Clean reward is `pending`; flagged rewards (`shared_domain`, `shared_origin`) are `held` | verified live | `42`, `47`, `48`, `49`, `50` |
| Release (note required, super admin only, `pending` result), release again 409, void, unknown 404 | verified live | `51` to `56` |
| Hold passes, reward `payable` (job function called with a shifted clock) | verified live (function, not cron) | `57` |
| Draft batch prepared; approve (manual) gives `awaiting_manual`; approve again idempotent | verified live | `58`, `59`, `61`, `62` |
| `mark-paid` amount mismatch 409 with `current_amount_kobo`; correct amount pays; repeat 409 | verified live | `63`, `64`, `65`, `66` |
| Marketer earnings buckets and payout history after payment | verified live | `33`, `42`, `57`, `68` |
| Marketer list status `signed_up` / `converted`, `trial_days_left` and `converted_at` null vs set, masked email | verified live | `37`, `41`, `69` |
| A voided reward's company is `not_converted` (R44); a paid company without a reward yet is `not_converted` until the reconcile creates it | not exercised live; tests | `test_a_void_reward_shows_not_converted_even_while_the_trial_is_open`. Capture `69` shows `signed_up` / `trial_days_left: 30` for the voided company because the scratch run's directly inserted payment did not upgrade the tenant (the real `upgrade_subscription` sets `active` and clears the trial), see section 6.2 |
| Config read, partial change, bounds 422 | verified live | `70`, `71`, `72` |
| Role matrix: plain admin reads 200 and writes 403; 401 for no, tenant, platform or wrong-kind tokens | verified live | `52`, `60`, `73` to `78`, `19` |
| Paystack Transfers (initiate, verify, webhooks), OTP transfers, batch retry | NOT exercised live | section 13 |
| Dispute hold and `reward_in_flight` 409 | NOT exercised live | section 13 |
| Emails (invite, reset, conversion, payout) | NOT exercised live | section 13 |
| `not_converted` status; 429 on portal and admin routes; bank PATCH; other mark-paid errors | NOT exercised live | code and tests, labelled inline |
| `mark-paid` accepts the claimed OR the current amount for an `awaiting_manual` item; a dispute on such an item keeps the reward locked (`reward_in_flight`); a third amount is 409 with both figures | NOT exercised live (added after the run); tests | `test_r49_*` in `tests/referral/test_payouts.py` and `test_payout_endpoints.py`, section 9.5 |
| `POST /batches/prepare` (draft or `{"batch": null}`, idempotent, super admin only, audited) | NOT exercised live (added after the run); tests | `tests/referral/test_payout_endpoints.py`, section 9.1A |
| Atomic audit for marketer create / bank / suspend / reactivate (`500` with a fixed code, nothing changed) | NOT exercised live; tests | `tests/referral/test_marketer_admin.py`, section 7.1 |
| Paystack-era `failed` / `processing` items are verified with Paystack after the provider is switched back to `manual` | NOT exercised live (mock HTTP); tests | `test_m5_*` in `tests/referral/test_payouts.py`, section 9.1 |
| SMTP socket timeout is 30 s | tests (constant asserted); not timed live | `tests/referral/test_notifications.py` |
