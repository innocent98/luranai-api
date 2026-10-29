# Referral program (marketer recruits): design

Status: **DRAFT for Adebayo's approval, 2026-10-28 (builder check done, no open questions).** Nothing here is built. Facts about the current code come from a read-only recon on 2026-10-28 and are cited as `file:line`. Where a statement is my call rather than Adebayo's decision it is marked **(my call)**.

## 1. Intent and why

Luran recruits marketers and influencers, who bring businesses to sign up. A marketer is not an employee: they earn a **one-time 5,000 naira commission per referred tenant that becomes a paying customer**, and nothing from that tenant's later subscription. If a marketer brings 100 tenants and 80 pay, they earn 80 x 5,000.

Why now: prod has 13 tenants, 0 payments ever and about 1.6 signups a month (pricing spec, 2026-10-27). Paid acquisition through people is the cheapest lever, and a commission paid only after money has been received carries no up-front cost.

PRD: `docs/luran_prd.md` has no referral feature, so this is **new scope**. Add a feature entry to the PRD when this design is approved.

## 2. Goals, non-goals, anti-goals

**Goals**
- Marketers are onboarded by Luran admins only. Nobody can self-enroll or obtain a code by guessing.
- Each marketer has one personal link, `https://luranai.com/register?ref=<code>`, and one single page that shows their link, their referred tenants, conversions, this month's earnings and what has been paid out.
- Attribution survives days between the click and the signup, in the same browser.
- Reward is created only by a real payment, protected against repeat or fake signups, paid automatically at month end after a one-click admin approval.

**Non-goals (v1)**
- Recurring commission, multi-level commission, tiered rates.
- Marketer self-signup, payout requests, a public leaderboard.
- Cross-device attribution, UTM analytics, click tracking beyond the attribution cookie.
- Refund or clawback logic. Luran gives **no refunds**; a marketer keeps a paid commission (Adebayo's decision).
- Marketers seeing anything about a tenant beyond what section 5 lists.

**Anti-goals (failure even if it "works")**
- A marketer, or anyone with a marketer login, can reach tenant data or any admin endpoint.
- The same tenant is rewarded twice (webhook replay, retry, two marketers).
- Money leaves without an admin's approval of that batch.
- A tenant that signed up more than once is rewarded each time.

## 3. Decisions taken by Adebayo

| # | Decision |
|---|---|
| D1 | Marketers are onboarded from the admin dashboard; links are not public. |
| D2 | One single-page dashboard per marketer: link, referred tenants (signed up / converted / not converted), this month's earnings, amount withdrawn (paid out). |
| D3 | Commission: 5,000 naira, one time per converted tenant. A tenant converts when it pays after its 30-day free trial. |
| D4 | Marketers cannot request payouts. The system calculates and prepares payouts automatically; an admin approves each batch with one click; the system then pays. |
| D5 | No refunds, so no clawback; paid commissions are kept. |
| D6 | Fraud: detect a tenant opening a second account for the same website (scan or knowledge base). A person with several different businesses is legitimate. |
| D7 | A marketer sees, per referred tenant, the **company name and a masked email**. |
| D10 | Marketers sign in through the normal tenant login page (no admin login) and are routed to their single page on success. |
| D8 | Attribution is recognised after days in the same browser, if technically possible (it is, see 6.1). |
| D9 | Hold window 7 days after the first payment; payout batches on the 1st and 15th of each month, each with one-click approval. |

## 4. Constraints from the current code (recon, 2026-10-28)

- Signup is `POST /api/v1/auth/register-company` (`app/api/v1/endpoints/company_registration.py:25-34`). `CompanyRegistrationRequest` has `extra="forbid"` (`app/schemas/auth.py:9-46`), so a new field must be added explicitly. It is rate-limited to 3 per hour per IP (`http_rate_limiter.py:79-83`). The trial is `now + 30 days` (`company_registration_service.py:94`).
- **No IP or user agent is stored at signup** (`register_company` receives only the body). Fraud signals based on network origin do not exist yet.
- The only existing code-carrying field is `claim_token` (a website-audit claim). An audit run before signup is re-parented to the new tenant (`company_registration.py:67-80`, `app/auditor/service.py:387`).
- Payments: `payment_transactions` (unique `reference`, `amount` in kobo, `status` pending/success/failed/abandoned, `paid_at`; `app/db/models/payment.py`). A row is created `pending` at checkout and flipped to `success` by `charge.success` in `WebhookHandlerService._handle_charge_success` (`webhook_handler_service.py:64-165`). **That handler is not idempotent**: a replay re-runs the upgrade and appends another history row. There is no stable "first payment" flag; the only reliable derivation is the earliest distinct `reference` with `status='success'` for the tenant.
- **No dispute or chargeback handling exists** (no `charge.dispute.*` handler). Renewal charges have no pre-created row and are skipped by the handler (`:89-91`); only the first payment is recorded, which suits a one-time commission.
- Manual admin overrides and promotional grants do not create `payment_transactions` rows, so they will not trigger rewards. (The existing Growth tenant is a manual override.)
- Roles: tenant roles live in `UserRole` (`user.py:16-21`); platform admins are a separate table `platform_admins` with `admin_type="platform_admin"` in the JWT (`platform_admin_auth_service.py:80-90`). Platform endpoints gate **per endpoint**, and `get_current_platform_admin` admits **any** platform role (`deps.py:474-519`), so adding a "marketer" value to the platform admin role enum would silently grant access to every endpoint that uses that dependency.
- No registrable-domain / public-suffix helper exists. `audit_reports.domain` is stored un-normalised (keeps `www.`, may keep a port; `app/auditor/service.py:105`). `knowledge_sources.source_url` is a raw URL. The widget host is stored on `tenants.widget_last_seen_host`, lowercased (`widget_seen_service.py:30-44`). `tenants` has no website column.
- No outbound payout code and no Paystack transfer calls exist (`paystack_service.py`: initialize, verify, subscriptions only). `EmailService.send_templated_email` can send invites.
- The existing "promotional grant" feature (`promotional_grant.py`) is a tier-elevation tool with a `campaign_code`. **Name collision risk: this program is always called "referral", never "promo".**

## 5. Design

### 5.1 Marketer identity and access (my call)

A **separate `marketers` table and a separate token type** (`admin_type="marketer"`), not a new platform-admin role. Reason: the recon shows existing platform endpoints would admit a new platform role by default. A separate identity cannot reach any tenant or admin dependency, because those dependencies check for other token types. Marketer endpoints live under `/api/v1/marketer/*` and use one new dependency, `get_current_marketer`.

**Where it lives (my call).** Both front ends are in the existing `luran-dashboard` repo (Next.js, served at luranai.com, the same host as `/register`, so the attribution cookie is first-party): the marketer page at `/marketer` with its own login, and the admin screens inside the existing platform-admin area. Branch topology applies as usual: dashboard feature -> `staging` -> `main`, backend feature -> `develop` -> `main`. Two streams (backend, dashboard), backend first.

**Code rules (my call).** 3-32 characters, lowercase letters, digits and hyphens; a reserved-word blocklist (for example `admin`, `api`, `login`, `register`, `luran`); unique case-insensitively; **immutable once issued** (to change a code, suspend the marketer's old code and issue a new one, so old links keep resolving to the same marketer).

`marketers`: id, name, email (encrypted, plus a hash for lookup, like `users`), password hash, `code` (unique, case-insensitive, lowercase letters/digits/hyphen, set by the admin when onboarding), status (`active` / `suspended`), bank details (bank code, account number, account name; encrypted at rest, entered by the admin at onboarding), created_by admin id, timestamps. **Login (Adebayo, 2026-10-28):** marketers use **the same login page as tenants**, never an admin login page. They sign in with their email and password and, on success, are routed to `/marketer`. Under the hood (my call) the marketer is still a separate identity: the login page first tries the normal tenant login; if the credentials are not a tenant user, it tries a separate marketer login endpoint (`POST /api/v1/auth/marketer/login`), so the core tenant authentication code is not touched. A successful marketer login sets its own cookies (`marketer_access_token`, `marketer_refresh_token`), distinct from the tenant and platform-admin cookie names, so a browser holding a tenant or admin session can never be confused with a marketer session. A marketer who then opens a tenant or admin URL is treated as not signed in. An email address cannot be both a tenant user and a marketer: creating a marketer with an email that already belongs to a tenant user is rejected (they use another email). Password reset reuses the normal reset flow's pattern with a marketer branch.

Admin onboarding: a platform admin creates the marketer; the system emails an invite with a set-password link (reuse `EmailService.send_templated_email`). Suspending a marketer stops new attribution to that code and holds unpaid rewards for review; it does not delete history.

### 5.2 Attribution

1. A visitor opens `https://luranai.com/register?ref=<code>` (or any luranai.com page with `?ref=`). A small client script stores the code in a first-party cookie `luran_ref` (**30 days, first link wins**, a later different code does not overwrite an unexpired one) (my call). The cookie holds only the public code.
2. The signup form sends it as a new optional field `referral_code` in `register-company`. A visible optional "Referral code" field on the form is the fallback when the cookie is missing (different browser or device, private window); the cookie value pre-fills it.
3. The backend validates the code against active marketers. An unknown, suspended or malformed code is **ignored** and never blocks signup. A valid code creates one row in `referral_attributions` (unique per tenant), with source `cookie` or `form`.
4. Not captured: cross-device journeys without typing the code. Disclose the cookie in the privacy policy.
5. Self-referral guard at attribution time: if the signup email or its domain equals the marketer's own, record the attribution but mark it `self_referral` and exclude it from rewards.

### 5.3 What a marketer sees (single page `/marketer`)

- Their link, a copy button, their code.
- A list of referred tenants: **company name, masked email** (`a***@d***.com`), signup date, and a status: **Signed up** (in trial, with day count), **Converted** (first payment date), **Not converted** (trial ended unpaid). For tenants in trial, two activation hints a marketer can act on: knowledge base uploaded yes/no, widget installed yes/no (existing onboarding data; no content).
- Totals: signed up, converted, conversion rate.
- Money: this month earned (converted this month), pending (in hold or under review), payable, and total paid out so far with a short payout history (date, amount, batch). All in naira.
- Nothing else: no tenant emails, no conversations, no plan names, no payment amounts.

### 5.4 Reward lifecycle

`referral_rewards` (unique per `tenant_id`): marketer_id, `amount_kobo` (snapshot of the configured amount, default 500,000), status, `converted_at`, `payable_after`, `review_reasons` (list), `payout_batch_id`, timestamps.

| Status | Meaning | Moves on when |
|---|---|---|
| `pending` | tenant paid for the first time | `payable_after` (= `converted_at` + 7 days) passes with no flag |
| `held` | fraud flag or dispute; needs an admin decision | admin releases (`pending`) or voids |
| `payable` | cleared the hold | included in the next batch |
| `batched` | in a payout batch awaiting approval | admin approves, payment succeeds |
| `paid` | transferred | final |
| `void` | not payable (self-referral, fraud confirmed, marketer removed before payment) | final |

**Release semantics (my call).** An admin releasing a `held` reward sends it to `payable` immediately if its `payable_after` date has already passed, otherwise back to `pending` with the original `payable_after` (the hold does not restart). It is then paid in the **next scheduled batch**, never off-cycle.

**Scheduling (my call).** A daily job in the existing worker moves `pending` rewards whose `payable_after` has passed and that have no flag to `payable`; a job on the 1st and 15th prepares the draft batch. Use the worker's existing scheduler, not a new mechanism.

**What counts as converting (my call).** The tenant's **first successful payment**, whenever it happens between signup and day 90, including a payment during the trial (an upgrade before day 30 also converts). On the marketer's page a tenant whose trial ended unpaid shows *Not converted* and turns *Converted* if it pays by day 90; after day 90 it stays *Not converted*. Manual admin overrides and promotional grants never count (they create no payment row).

Creation is triggered **inside the same transaction that records the tenant's first successful payment** and is made idempotent by the unique `tenant_id` constraint, so a webhook replay cannot create a second reward. The existing handler's lack of idempotency (a replay re-runs the tier upgrade and appends another history row) is a separate bug on a money path; referral correctness does not depend on it, and a small guard that returns early for a reference already marked `success` is recommended as its own PR before or alongside this work. The reward exists only if the tenant has an attribution and the first payment falls **within 90 days of signup** (my call; later "winback" payments are not commissionable). Trial tenants who never pay produce nothing.

If a dispute event arrives while a reward is `pending`/`payable`, it moves to `held`. A dispute after `paid` changes nothing (Adebayo's no-clawback rule). Because no dispute handler exists today, add one for Paystack's dispute event (**verify the exact event name against Paystack's current docs**), limited to flagging the reward.

### 5.5 Fraud detection ("is this tenant already with us?")

Principle: **flag and hold, never silently block a signup**, because one person can legitimately run several businesses. The reward is where money is at stake, so that is where the check bites. Signals are gathered at two moments, signup and conversion, because most of them (knowledge-base URL, widget host) only exist after signup.

1. **Same website.** Normalise every website a tenant has to a registrable domain (lowercase, scheme and port stripped, `www.` removed, reduced with a public-suffix list so `shop.example.com.ng` becomes `example.com.ng`). Sources: claimed audit `domain`, URL knowledge sources `source_url`, `widget_last_seen_host`. At conversion time, compare the tenant's set against every other tenant's set; **any shared registrable domain with another tenant is a strong flag** and the reward is `held` with the reason `shared_domain`. Different domains, even for the same owner, pass.
2. **Same card.** The Paystack authorization `signature` (stable per card) is already inside the stored raw event (`paystack_data`); the same signature on two different tenants referred by the same marketer is a flag `shared_card` (**verify the field exists in the stored payload**).
3. **Same origin.** Store a salted hash of IP and user agent at signup (new; privacy-minimal, never the raw values) and flag several referred tenants from one marketer sharing it, `shared_origin`. Weak signal: reasons are shown to the reviewer, not auto-actioned.
4. **Marketer-tenant link.** Same email domain or email hash between marketer and tenant (`self_referral`).
5. **Rate.** Many referred signups by one marketer in a short window flags the marketer's next rewards for review.

Honest limits: the same business under a different domain, or a fresh domain bought to fool the check, is not caught automatically. That is why flagged rewards go to a human, the 7-day hold exists, and Luran only pays for money it has actually received (a fake tenant must pay at least the cheapest plan, 15,000 naira, to earn 5,000).

The domain-matching and the flags also feed a lightweight admin view (5.7). The same normalisation can later power duplicate-account detection for other purposes; out of scope here.

### 5.6 Payouts

- Cadence: batches are prepared automatically on the **1st and 15th** (my call from D9): every `payable` reward for each active marketer is grouped into a draft `payout_batches` row with per-marketer totals.
- An admin reviews the batch (marketer, count, total, any held rewards left out) and clicks **Approve**. Nothing moves before that.
- On approval the system pays each marketer's registered bank account and marks rewards `paid` with the transfer reference, idempotently (one reference per marketer per batch; a retry never double-pays). Failed transfers leave the reward `batched` and visible for retry.
- Build the payout step behind a small **payout provider interface** (my call): a manual implementation (export the approved batch, admin pays by bank transfer and records the reference with "mark as paid") ships first so nothing blocks, and a Paystack Transfers implementation plugs into the same interface when the account is ready. The reward lifecycle, schema (transfer reference, recipient code nullable) and marketer page are identical either way.
- Mechanism (decided, section 8): Paystack Transfers via API (transfer recipients created from the marketer's bank details, a funded balance, the account's transfer-approval handling). If a transfer fails or the balance is short, the batch item stays `batched` and the admin can retry, or export the batch and pay manually with "mark as paid".
- Minimum payout threshold: none in v1 (my call).

### 5.7 Admin dashboard (platform admin)

- Marketers: create (name, email, code, bank details), edit bank details, suspend/reactivate, see their numbers.
- Reward review queue: held rewards with reasons (shared domain with which tenant names, shared card, etc.); actions Release or Void with a note.
- Payout batches: list, open, Approve, see transfer results.
- Config: reward amount, hold days, attribution window, commission window (90 days), batch days.

### 5.8 Data and migrations

New tables: `marketers`, `referral_attributions`, `referral_rewards`, `payout_batches` (and a batch-items link or `payout_batch_id` on rewards), `signup_origins` (hashed IP/UA per tenant; or two columns on an existing signup record, builder's choice). One additive Alembic migration chain, each step with `lock_timeout`, reversible. Amounts are integer kobo. No change to existing tables except one optional field on the signup request schema.

### 5.9 Security and privacy

- Marketer endpoints return only the fields in 5.3. Masked email computed server-side; the unmasked address is never sent.
- Bank details encrypted at rest and never returned by any API after entry (show last 4 digits only).
- Marketer tokens cannot satisfy any tenant or platform dependency (tested both ways).
- Rate-limit marketer login; no enumeration (generic errors).
- The referral cookie holds a public code only. Add the cookie and the commission program to the privacy policy.
- Never log codes with tenant identifiers together with bank details; never print env or secrets.

## 6. Verification expected of the build

- A webhook replay creates exactly one reward; two marketers' links cannot both claim a tenant.
- A marketer token gets 403 on every platform and tenant endpoint; a platform or tenant token gets 403 on `/marketer/*`.
- Signup with an unknown/suspended code succeeds with no attribution.
- Shared-domain, shared-card, self-referral flags each produce `held` with the right reason; different domains do not.
- Batch preparation, approval, a failed transfer and a retry never double-pay.
- FE integration guide with real captured responses from the running app; SOP; checklist entry.

## 7. Left to the builder

Library choice for public-suffix reduction (vet the dependency; offline snapshot, no network at runtime); table layout details; the exact masking function; the admin UI layout; the shape of the review reasons; how the cookie script is placed on the marketing pages.

## 8. Decided by Adebayo, with what it leaves for him

1. **Payouts use Paystack Transfers.** Adebayo confirmed on 2026-10-28 that API transfers are enabled on Luran's Paystack account. v1 therefore ships the Paystack implementation behind the provider interface, with the manual "mark as paid" implementation kept as the fallback for a failed transfer or an unfunded balance. Owner actions before the first live batch (not blockers for building): confirm that API-initiated transfers do not require an OTP per transfer (or choose how that approval is handled), keep the Paystack balance funded for each batch, and test one small transfer in test mode first.

No open questions remain.

## 9. Calls made in this document (marked "my call" above)

| Topic | Call |
|---|---|
| Marketer identity | Separate `marketers` table and `admin_type="marketer"` token, not a platform-admin role; signs in on the normal login page via a separate marketer login endpoint and own cookie names |
| Where it lives | `luran-dashboard` (marketer page `/marketer` + admin screens), backend `luran` |
| Attribution | `luran_ref` cookie, 30 days, first link wins; form field fallback; unknown code ignored |
| Commission window | First successful payment between signup and day 90, including during the trial |
| Release of held rewards | To `payable` at once if the hold has passed, else back to `pending` with the original date; paid in the next batch |
| Scheduler | Existing worker: daily pending-to-payable job, batch prep on the 1st and 15th |
| Code format | 3-32 chars, lowercase/digits/hyphen, reserved words blocked, immutable once issued |
| Minimum payout | None in v1 |
| Payout step | Provider interface; Paystack Transfers in v1 (enabled, per Adebayo), manual "mark as paid" as fallback |
| Webhook idempotency | Separate small PR recommended, not part of this feature |
| Emails | Invite, conversion notice and payout notice to the marketer (small; strike if unwanted) |
