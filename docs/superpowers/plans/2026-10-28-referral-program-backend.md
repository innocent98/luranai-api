# Referral Program (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Marketers are onboarded by admins, get a referral link, are credited a one-time 5,000 naira per referred tenant that makes a first payment, and are paid in admin-approved batches via Paystack Transfers, with duplicate-business fraud held for review.

**Architecture:** New `marketers` identity (separate table, token type and cookies) so no tenant or admin dependency can admit it. Attribution rides on `register-company`; a reward row (unique per tenant) is created in the transaction that records the tenant's first successful payment; a daily job clears the hold, a 1st/15th job drafts payout batches, and an admin approval pays through a provider interface (Paystack Transfers, manual fallback).

**Tech Stack:** FastAPI, SQLAlchemy 2 (async-compatible models as in `app/db/models/`), Alembic, arq worker cron (`app/workers/document_processor.py:1362`), Paystack (`app/services/paystack_service.py`), pytest. Branch `feature/referral-backend` off `origin/develop`, PR to `develop`, never merged by the implementer.

**Spec:** `docs/superpowers/specs/2026-10-28-referral-program-design.md` (read it first; section numbers below refer to it). The dashboard work is a separate plan, written after this backend has live captures.

## Global Constraints

- Reward: `REFERRAL_REWARD_KOBO=500000`, one per tenant (unique `tenant_id`), integer kobo, NGN only.
- Hold `REFERRAL_HOLD_DAYS=7`; commission window `REFERRAL_COMMISSION_WINDOW_DAYS=90` from signup; attribution cookie `luran_ref` 30 days, first link wins (frontend); all four are settings in `app/core/config.py`.
- Batches prepared on the 1st and 15th; nothing is paid without an admin approval of that batch; no payout requests by marketers; no clawback after `paid`.
- Marketer sees only: code, link, referred tenants (company name, masked email like `a***@d***.com`, signup date, status, trial day count, knowledge-base-uploaded yes/no, widget-installed yes/no), totals, earnings, payout history. Never raw emails, plan names, payment amounts or conversations.
- Marketer cookies are `marketer_access_token` / `marketer_refresh_token`; token claim `admin_type="marketer"`; marketer endpoints under `/api/v1/marketer/*`, login under `/api/v1/auth/marketer/*`, admin screens under `/api/v1/platform/referrals/*`.
- Code format: 3-32 chars `[a-z0-9-]`, unique case-insensitive, reserved words blocked (`admin`, `api`, `login`, `register`, `luran`, `support`, `www`), immutable once issued.
- Never name this feature "promo" (collides with `promotional_grant.py`). No AI attribution in commits or PR text. Never print env or secret values. Reproduce CI locally with poetry-pinned `ruff 0.8.6` and `black 24.10.0` plus the full pytest suite before pushing.
- Bank details are encrypted at rest (`EncryptedString`, `app/core/encryption.py`) and never returned by any API (last 4 digits only).

## Review Focus

- A Paystack `charge.success` replay or two webhook deliveries in parallel must create exactly one reward and one history row (Task 1, Task 5).
- A suspended marketer's unpaid rewards are held and its code stops attributing; a tenant already attributed stays attributed (Task 2, Task 4).
- Signup with an unknown, suspended, malformed or over-long `referral_code` still succeeds with no attribution and no error text that reveals which codes exist (Task 4).
- Two tenants sharing a registrable domain differing only by `www.`, scheme, port, case or a subdomain are matched; `example.com.ng` is not treated as the registrable domain `com.ng` (Task 6).
- Approving the same batch twice, or retrying after a partial Paystack failure, never transfers twice (Task 8).

---

### Task 1: Make `charge.success` idempotent (separate small PR, merge first)

**Files:** Modify `app/services/webhook_handler_service.py:64-165` (`_handle_charge_success`); Test `tests/services/test_webhook_charge_success_idempotent.py`.

**Interfaces:** Produces: `_handle_charge_success` returns `{"status": "already_processed"}` and performs no side effects when the transaction row for `reference` already has `status == "success"`.

- [ ] **Step 1: Write failing tests** `test_replay_does_not_rerun_upgrade`, `test_replay_does_not_add_history_row`, `test_first_delivery_unchanged`: deliver the same `charge.success` payload twice with a pending row; assert one `SubscriptionHistory` upgrade row, `paid_at` unchanged on the second call, tenant tier unchanged.
- [ ] **Step 2: Run** `poetry run pytest tests/services/test_webhook_charge_success_idempotent.py -v`; expect FAIL on the replay tests.
- [ ] **Step 3: Implement** the early return, placed after the row lookup and under the row lock (`SELECT ... FOR UPDATE` on the transaction) so two parallel deliveries serialise.
- [ ] **Step 4: Run** the new tests and `tests/services -k webhook`; expect PASS.
- [ ] **Step 5: Commit** `fix(billing): make charge.success idempotent`. Open as its own PR to `develop`.

### Task 2: Settings, models and migration

**Files:** Create `app/db/models/referral.py`, `alembic/versions/ref10_referral_program.py` (`revision="ref10referral"`, `down_revision="wa20ratingreply"`, `SET LOCAL lock_timeout = '5s'`, reversible, existence-checked like `ent10_plan_entitlements.py`); Modify `app/core/config.py` (the four settings), `app/db/models/__init__.py`; Test `tests/referral/test_models_migration.py` (scratch-Postgres pattern in `tests/migrations`).

**Interfaces:** Produces enums `MarketerStatus{active,suspended}`, `AttributionSource{cookie,form}`, `RewardStatus{pending,held,payable,batched,paid,void}`, `BatchStatus{draft,approved,paying,paid,failed}`; models `Marketer(id, name, email EncryptedString, email_hash, password_hash, code, status, bank_code, account_number, account_name [EncryptedString], paystack_recipient_code nullable, created_by_admin_id, created_at)`, `ReferralAttribution(tenant_id unique, marketer_id, source, self_referral bool, attributed_at)`, `ReferralReward(tenant_id unique, marketer_id, amount_kobo, status, converted_at, payable_after, review_reasons JSON list, payout_batch_id nullable, paid_at nullable, transfer_reference nullable)`, `PayoutBatch(id, period_label, status, total_kobo, approved_by_admin_id, approved_at)`, `SignupOrigin(tenant_id unique, origin_hash)`. Unique index on `lower(code)`; `email_hash` set by the same `hash_for_lookup` listener pattern as `User`.

- [ ] **Step 1: Write failing tests** `test_upgrade_creates_tables_and_downgrade_removes_them`, `test_second_reward_for_same_tenant_rejected` (IntegrityError), `test_code_unique_case_insensitive`, `test_bank_fields_encrypted_at_rest` (raw column value differs from plaintext).
- [ ] **Step 2: Run** `poetry run pytest tests/referral/test_models_migration.py -v`; expect FAIL.
- [ ] **Step 3: Implement** models, settings and migration; verify single alembic head (`poetry run alembic heads`).
- [ ] **Step 4: Run** tests plus `tests/migrations`; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): models, settings, migration ref10referral`.

### Task 3: Marketer admin service and platform endpoints

**Files:** Create `app/services/referral/codes.py`, `app/services/referral/masking.py`, `app/services/referral/marketer_service.py`, `app/schemas/referral.py`, `app/api/v1/endpoints/platform/referrals.py` (mount in `app/api/v1/api.py` under `/platform/referrals`); Modify `app/services/email_service.py` (add `send_marketer_invite_email`); Test `tests/referral/test_marketer_admin.py`, `tests/referral/test_codes_masking.py`.

**Interfaces:** Produces `validate_marketer_code(code: str) -> str` (returns normalised lowercase, raises `ValueError`), `mask_email(email: str) -> str`, `MarketerService.create(admin_id, name, email, code, bank_code, account_number, account_name) -> Marketer` (rejects an email that exists as a tenant `User`, sends the invite), `suspend(marketer_id)`, `reactivate(marketer_id)`, `update_bank_details(...)`. Endpoints (all `Depends(get_current_platform_admin)`): `POST /`, `GET /`, `GET /{id}`, `PATCH /{id}/bank`, `POST /{id}/suspend`, `POST /{id}/reactivate`. Responses expose `bank_last4` only. Suspending moves that marketer's `pending`/`payable` rewards to `held` with reason `marketer_suspended`.

- [ ] **Step 1: Write failing tests** `test_code_rules` (length 2 and 33 rejected, uppercase normalised, `admin` rejected, `a_b` rejected), `test_mask_email` (`mask_email("ada@example.com") == "a***@e***.com"`; `mask_email("x@y.co.ng") == "x***@y***.co.ng"`: first character of the local part and of the first domain label kept, each followed by `***`, everything after the first dot of the domain kept; the full local part never appears), `test_create_rejects_tenant_user_email`, `test_create_duplicate_code_case_insensitive_409`, `test_response_never_contains_full_account_number`, `test_non_admin_token_403`, `test_suspend_holds_unpaid_rewards`.
- [ ] **Step 2: Run** `poetry run pytest tests/referral/test_codes_masking.py tests/referral/test_marketer_admin.py -v`; expect FAIL.
- [ ] **Step 3: Implement** per the interfaces; the invite email carries a one-time set-password link built like the existing password-reset token flow (`token_service.py`).
- [ ] **Step 4: Run** the tests; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): marketer onboarding from the admin API`.

### Task 4: Marketer authentication and isolation

**Files:** Create `app/services/referral/marketer_auth_service.py`, `app/api/v1/endpoints/marketer_auth.py` (mount `/auth/marketer`), add `get_current_marketer` to `app/api/deps.py`, rate limiter `marketer_auth_rate_limiter` in `app/utils/http_rate_limiter.py`; Test `tests/referral/test_marketer_auth.py`.

**Interfaces:** Produces `POST /auth/marketer/login` (email, password -> sets cookies via `set_auth_cookies(..., access_cookie="marketer_access_token", refresh_cookie="marketer_refresh_token", refresh_path="/api/v1/auth/marketer/refresh")`, body `{"role": "marketer", "redirect": "/marketer"}`), `POST /auth/marketer/refresh`, `POST /auth/marketer/logout` (revokes the refresh token as the tenant logout does), `POST /auth/marketer/set-password` (token from the invite), `get_current_marketer() -> Marketer` (requires `admin_type == "marketer"` and `status == active`). Generic `401 invalid_credentials` for unknown email and wrong password alike.

- [ ] **Step 1: Write failing tests** `test_login_sets_only_marketer_cookies`, `test_suspended_marketer_cannot_login`, `test_marketer_token_rejected_by_tenant_admin_dependency`, `test_marketer_token_rejected_by_platform_admin_dependency`, `test_tenant_and_platform_tokens_rejected_by_marketer_endpoints`, `test_unknown_email_and_wrong_password_identical_response`, `test_login_rate_limited`, `test_logout_revokes_refresh`.
- [ ] **Step 2: Run** `poetry run pytest tests/referral/test_marketer_auth.py -v`; expect FAIL.
- [ ] **Step 3: Implement** mirroring `platform_admin_auth_service.py:80-90` for claims; the same-page login behaviour is frontend-only (tries tenant login, then this endpoint).
- [ ] **Step 4: Run** tests; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): marketer login, tokens, isolation`.

### Task 5: Attribution at signup

**Files:** Modify `app/schemas/auth.py:9-46` (add `referral_code: str | None = Field(default=None, max_length=64)`), `app/api/v1/endpoints/company_registration.py`, `app/services/company_registration_service.py`; Create `app/services/referral/attribution.py`; Test `tests/referral/test_attribution.py`.

**Interfaces:** Produces `attribute_signup(db, tenant: Tenant, admin_email: str, referral_code: str | None, source: AttributionSource, origin_hash: str) -> ReferralAttribution | None` (never raises on bad input; returns None for unknown/suspended/malformed; sets `self_referral=True` when the signup email equals the marketer's email or shares its email domain, excluding common mail providers `gmail.com`, `yahoo.com`, `outlook.com`, `hotmail.com`, `icloud.com`) and `origin_hash(ip: str, user_agent: str) -> str` (HMAC-SHA256 with a server secret from settings, never the raw values), stored in `SignupOrigin`. `register_company` gains `referral_code` and request IP/UA parameters; attribution runs after the tenant commit and inside try/except so it can never block signup.

- [ ] **Step 1: Write failing tests** `test_valid_code_creates_attribution`, `test_unknown_suspended_malformed_and_overlong_codes_signup_still_201_no_attribution`, `test_error_body_identical_for_known_and_unknown_codes`, `test_self_referral_marked_and_gmail_not_treated_as_same_domain`, `test_origin_hash_not_reversible_and_no_raw_ip_stored`, `test_extra_forbid_still_rejects_unknown_fields`.
- [ ] **Step 2: Run** `poetry run pytest tests/referral/test_attribution.py -v`; expect FAIL.
- [ ] **Step 3: Implement** per the interfaces.
- [ ] **Step 4: Run** tests and `tests -k registration`; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): attribute signups to marketers`.

### Task 6: Domain matching and fraud flags

**Files:** Create `app/services/referral/domains.py`, `app/services/referral/fraud.py`; add the public-suffix dependency (vet it: offline snapshot, no runtime network, pinned version; `tldextract` with `suffix_list_urls=()` is the default candidate); Test `tests/referral/test_domains.py`, `tests/referral/test_fraud_flags.py`.

**Interfaces:** Produces `registrable_domain(value: str) -> str | None` (accepts a URL or bare host; lowercases, strips scheme, port, path, `www.`; reduces via the public-suffix list), `tenant_domains(db, tenant_id) -> set[str]` (from claimed `audit_reports.domain`, `knowledge_sources.source_url` of type url, `tenants.widget_last_seen_host`), `evaluate_flags(db, tenant_id) -> list[str]` returning any of `shared_domain`, `shared_card`, `shared_origin`, `self_referral`, `marketer_burst` (more than 10 referred signups by one marketer in 24 h). `shared_domain` compares against every other tenant; `shared_card` compares the Paystack authorization `signature` found in the stored `paystack_data` (verify the key exists in a real stored payload before relying on it; if absent, drop this flag and note it in the SOP).

- [ ] **Step 1: Write failing tests** `test_registrable_domain` (cases: `https://WWW.Shop.Example.com.ng:8080/x` -> `example.com.ng`; `example.com` -> `example.com`; `foo.co.uk` -> `foo.co.uk`; `localhost` and `192.168.1.1` -> `None`), `test_tenants_with_www_and_apex_match`, `test_different_domains_same_owner_not_flagged`, `test_com_ng_alone_is_not_a_match_between_unrelated_tenants`, `test_each_flag_reason_returned`, `test_no_flags_for_clean_tenant`.
- [ ] **Step 2: Run** the two files; expect FAIL.
- [ ] **Step 3: Implement.** Normalise and compare in Python on the domain sets (data is small); do not add a new table.
- [ ] **Step 4: Run** tests; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): registrable-domain matching and fraud flags`.

### Task 7: Reward creation, hold clearing and disputes

**Files:** Create `app/services/referral/rewards.py`; Modify `app/services/webhook_handler_service.py` (call inside the `charge.success` transaction; add a dispute branch to the event dispatch at `:39-46`); Modify `app/workers/document_processor.py` (cron registration, daily 03:00 UTC) with the job in `app/workers/referral.py`; Test `tests/referral/test_rewards.py`, `tests/workers/test_referral_jobs.py`.

**Interfaces:** Produces `create_reward_for_first_payment(db, tenant_id, payment_reference: str, paid_at: datetime) -> ReferralReward | None` (None unless: attribution exists and is not `self_referral`, marketer exists, no reward yet, `paid_at <= tenant.created_at + 90 days`; sets `payable_after = paid_at + 7 days`; evaluates flags and sets `status=held` plus `review_reasons` when any), `release(db, reward_id, admin_id, note: str)` (to `payable` if `payable_after` passed else `pending`, same date), `void(db, reward_id, admin_id, note: str)`, `promote_pending(db, now: datetime) -> int` (pending with `payable_after <= now` and no flags -> payable; also the arq job `referral_promote_pending`), `hold_for_dispute(db, tenant_id)` (pending/payable/batched -> held reason `payment_dispute`; no effect on `paid`). Dispute event name: verify against Paystack's current docs (expected `charge.dispute.create`) and log-and-ignore unknown events.

- [ ] **Step 1: Write failing tests** `test_first_payment_creates_one_reward_amount_500000`, `test_replay_and_parallel_delivery_one_reward`, `test_second_payment_creates_nothing`, `test_payment_after_day_90_creates_nothing`, `test_payment_during_trial_counts`, `test_admin_override_or_promotional_grant_creates_nothing`, `test_unattributed_and_self_referral_create_nothing`, `test_flagged_goes_held_with_reasons`, `test_promote_pending_respects_7_day_hold`, `test_release_semantics_before_and_after_hold`, `test_dispute_holds_unpaid_but_not_paid`.
- [ ] **Step 2: Run** `poetry run pytest tests/referral/test_rewards.py tests/workers/test_referral_jobs.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Reward creation shares the webhook's DB transaction; the unique `tenant_id` constraint is the last line of defence (catch `IntegrityError` and treat as already rewarded).
- [ ] **Step 4: Run** tests plus `tests/services -k webhook`; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): reward lifecycle on first payment`.

### Task 8: Payout batches and providers

**Files:** Create `app/services/referral/payouts.py`, `app/services/referral/providers.py`; Modify `app/services/paystack_service.py` (add `create_transfer_recipient`, `initiate_transfer`, `verify_transfer`, and transfer-event handling in the webhook dispatcher; verify exact endpoint paths and event names `transfer.success` / `transfer.failed` / `transfer.reversed` against Paystack's current docs); Add `referral_prepare_batch` cron (1st and 15th, 04:00 UTC) in `app/workers/referral.py`; extend `app/api/v1/endpoints/platform/referrals.py`; Test `tests/referral/test_payouts.py`.

**Interfaces:** Produces `prepare_batch(db, now) -> PayoutBatch | None` (groups `payable` rewards of `active` marketers, marks them `batched`, status `draft`; None when nothing payable), `approve_batch(db, batch_id, admin_id) -> PayoutBatch` (draft -> approved -> paying; idempotent: a second call on a non-draft batch is a no-op returning the batch), `class PayoutProvider(Protocol): def pay(self, marketer: Marketer, amount_kobo: int, reference: str) -> TransferResult`, `PaystackTransferProvider` (creates and stores `paystack_recipient_code` once; `reference = f"ref-{batch_id}-{marketer_id}"` so a retry reuses it), `ManualPayoutProvider` (marks items awaiting a human; `POST /platform/referrals/batches/{id}/mark-paid` records the bank reference). Endpoints (platform admin): `GET /batches`, `GET /batches/{id}`, `POST /batches/{id}/approve`, `POST /batches/{id}/retry`, `POST /batches/{id}/mark-paid`. A transfer result `success` marks that marketer's rewards `paid` with `paid_at` and `transfer_reference`; `failed` leaves them `batched`; a batch is `paid` only when every item is paid. Provider selected by setting `REFERRAL_PAYOUT_PROVIDER` (`paystack` | `manual`, default `manual` until the owner checklist is done).

- [ ] **Step 1: Write failing tests** `test_prepare_batch_groups_by_marketer_and_skips_held_and_suspended`, `test_approve_twice_transfers_once` (fake provider counts calls), `test_partial_failure_then_retry_pays_only_failed_with_same_reference`, `test_batch_not_paid_until_all_items_paid`, `test_nothing_moves_without_approval`, `test_paystack_provider_builds_expected_requests` (HTTP mocked; assert amount in kobo and the stable reference), `test_manual_mark_paid_records_reference`, `test_transfer_webhook_signature_checked`.
- [ ] **Step 2: Run** `poetry run pytest tests/referral/test_payouts.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Take a row lock on the batch inside `approve_batch`; one provider call per (batch, marketer) guarded by a stored per-item status, not by memory.
- [ ] **Step 4: Run** tests; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): payout batches, Paystack and manual providers`.

### Task 9: Review queue and marketer API

**Files:** Create `app/api/v1/endpoints/marketer.py` (mount `/marketer`), `app/services/referral/marketer_views.py`; extend `platform/referrals.py` with `GET /rewards?status=held`, `POST /rewards/{id}/release`, `POST /rewards/{id}/void`, `GET/PATCH /config`; Test `tests/referral/test_marketer_api.py`, `tests/referral/test_review_queue.py`.

**Interfaces:** Produces (all `Depends(get_current_marketer)`) `GET /marketer/me` -> `{name, code, link: "https://luranai.com/register?ref=<code>"}`, `GET /marketer/referrals` -> `{items:[{company_name, masked_email, signed_up_at, status, trial_days_left, kb_uploaded, widget_installed, converted_at}], totals:{signed_up, converted, conversion_rate}}` with `status in {signed_up, converted, not_converted}` derived as in spec 5.4, `GET /marketer/earnings` -> `{this_month_earned_kobo, pending_kobo, payable_kobo, paid_total_kobo, payouts:[{date, amount_kobo, batch}]}` (this month earned = `converted_at` in the current calendar month, Africa/Lagos). Admin queue items include reasons and, for `shared_domain`, the names of the other tenants involved.

- [ ] **Step 1: Write failing tests** `test_referrals_list_has_only_allowed_fields` (assert exact key set; no unmasked email anywhere in the JSON text), `test_marketer_sees_only_own_tenants`, `test_status_derivation_signed_up_converted_not_converted_and_late_conversion`, `test_earnings_month_boundaries_lagos_time`, `test_kb_and_widget_hints_match_onboarding_status`, `test_release_and_void_require_note_and_platform_admin`, `test_config_patch_validates_ranges`.
- [ ] **Step 2: Run** the two files; expect FAIL.
- [ ] **Step 3: Implement.** Reuse the onboarding service for the two hints; do not duplicate its queries.
- [ ] **Step 4: Run** tests; expect PASS.
- [ ] **Step 5: Commit** `feat(referral): marketer API and admin review queue`.

### Task 10: Notifications, live captures, docs and final checks

**Files:** Modify `app/services/email_service.py` (`send_referral_conversion_email`, `send_referral_payout_email`, called from Task 7 and Task 8 on success, failures logged not raised); Create `scripts/capture_referral.py`, `docs/fe-integration-guide-referral.md`, `docs/fe-integration-captures/referral/`, `docs/sop/referral_program_backend.md`; Modify `docs/checklist/master-checklist.md`, `docs/luran_prd.md` (new feature entry), workspace checklist pointer.

- [ ] **Step 1: Write failing tests** `test_conversion_email_sent_once_per_reward`, `test_email_failure_does_not_roll_back_reward_or_payout`.
- [ ] **Step 2: Run** them; expect FAIL; implement; expect PASS.
- [ ] **Step 3: Run the capture script against a local running app** (scratch Postgres 15 and Redis 7 on spare ports, not the other project's containers) exercising: admin creates marketer, invite, marketer login, signup with `referral_code`, first payment webhook (signed test payload), held and clean rewards, batch prepare, approve, mark-paid; save request and response bodies verbatim; build the FE guide only from them, marking anything not exercised live (Paystack Transfers against the real API, dispute event) as such. Placeholders in examples must be obviously fake.
- [ ] **Step 4: SOP and checklist** per the global rules; list the owner actions (API transfer OTP handling, funded balance, one small test transfer, privacy-policy cookie line).
- [ ] **Step 5: Local CI parity:** the exact steps of `.github/workflows/ci.yml` with the poetry-pinned toolchain (`ruff` 0.8.6, `black` 24.10.0, full pytest, SQLite regression job); expect all green.
- [ ] **Step 6: Opus security review** of Tasks 4, 5, 7 and 8 (identity isolation, money path, idempotency); fix findings; then open the PR to `develop` and stop. Commit `docs(referral): FE guide, SOP, checklist`.

---

## Self-review

- **Spec coverage:** 5.1 -> Tasks 3, 4; 5.2 -> Task 5 (cookie and form field are the frontend plan); 5.3 -> Task 9; 5.4 -> Tasks 1, 7; 5.5 -> Tasks 5, 6, 7; 5.6 -> Task 8; 5.7 -> Tasks 3, 8, 9; 5.8 -> Task 2; 5.9 -> Tasks 3, 4, 9, Global Constraints; emails (calls table) -> Task 10; verification list in spec section 6 -> the tests named above.
- **Not in this plan (by design):** the dashboard pages, the `luran_ref` cookie script, the login-page fallback, the privacy-policy text: the dashboard plan, written once these endpoints have live captures.
- **Type consistency:** `RewardStatus`, `BatchStatus`, `create_reward_for_first_payment`, `PayoutProvider.pay`, `mask_email` and the cookie names are used with the same spelling across tasks.
- **Proportion:** the plan states signatures, values and tests; bodies are left to the implementer.
