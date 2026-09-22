# Account Deletion (design)

**Status:** approved design, 2026-09-22 — spec for user review. Spans **`luran`** (backend: endpoints + lifecycle + purge worker) and **`luran-dashboard`** (public request page + authed self-serve). No agent-app work this round.

**Driver:** Google Play's account-deletion policy + GDPR/CCPA right-to-erasure. Requires users to be able to request deletion of their account and personal data, including a **publicly reachable URL** (no login needed). *Not legal advice — the data policy below should be confirmed with whoever owns legal/compliance.*

## 1. Semantics — who deletes what (multi-tenant)
Accounts are `User` rows with `role ∈ {tenant_admin, support_agent}`, each scoped to a `tenant_id`. A tenant admin owns the workspace; a support agent is a member of one.
- **Support agent** deletes their own account → removes only their own `User` (their membership). Workspace and all its data untouched.
- **Tenant admin** deletes their own account → deletes **only their own admin `User`, *if another tenant_admin remains*** in the tenant.
  - **Last-admin guard:** if they are the sole remaining `tenant_admin`, self-deletion is **blocked** and the flow offers a choice: **(a)** transfer/promote another member to admin first, then delete self; or **(b)** explicitly delete the **entire tenant** (a separate, clearly-labeled destructive action that cascades ALL tenant data + all member users).
- **Whole-tenant deletion** (only reachable via the last-admin explicit path above): cascades sessions, tickets, leads, KB, config, sales config, devices, and all member `User`s of that tenant.

## 2. Lifecycle — request → verify → grace → purge (irreversible-safe)
1. **Request** created (authed self-serve, or public-by-email). A `deletion_requests` record is created with `status = pending_verification` (public) or `grace` (authed, identity already proven), `scheduled_purge_at = now + GRACE`.
2. **Disable:** the target `User` is marked `deletion_status` (login blocked; existing sessions revoked via the existing Redis `revoked:{jti}` mechanism).
3. **Verify** (public path): an email with a signed, expiring verification link (reuse the existing email/verification infra behind forgot-password / resend-verification). Clicking it moves the request `pending_verification → grace`. The authed path skips this (session already proves identity), but still emails a confirmation + cancel link.
4. **Grace period = 30 days** (configurable via env, e.g. `ACCOUNT_DELETION_GRACE_DAYS`). During grace the request is **cancellable** → account restored (`deletion_status` cleared, login re-enabled). Cancel via a link in the email and/or re-login prompt.
5. **Purge** (after grace): a scheduled ARQ worker (`account_deletion_purge`, mirrors the existing `conversation_auto_resolver` cadence) processes requests where `scheduled_purge_at <= now` and `status = grace` → applies the data policy in §3 → sets `status = purged`.

## 3. Data policy — HARD DELETE after grace, nothing retained
**Decision 2026-09-22 (supersedes an earlier anonymize+retain draft):** on purge, **hard-delete all of the user's personal data — no retained legal/billing subset, no retention period, no legal-signoff field list.** This deliberately keeps it simple.
- **`purge_user`:** remove ALL of the user's PII — `full_name`, `email`, `email_hash`, `phone`, avatar, auth credentials/tokens, device push tokens, authored personal free-text. Either delete the `User` row or leave a fully PII-free shell (whichever is cleanest given inbound FK constraints); the binding requirement is that **no PII remains** for that user. Idempotent.
- **Tenant-owned records are kept, de-identified — NOT deleted:** the tickets a support agent was assigned to and the customer-conversation messages they authored belong to the tenant and its customers. Keep them, but strip the person: `assigned_to` → NULL, author attribution → a neutral label ("Former agent"). Deleting them would destroy the tenant's customer-support history.
- **`purge_tenant`** (last-admin explicit path): full hard **cascade delete** of ALL tenant-scoped data + all member users — everything goes.
- Keep a **PII-free deletion audit** (that a purge ran + when) so it's provable; no personal data in it.
- The UI states plainly that the account and personal data are deleted after the grace period.

## 4. Surfaces + endpoints (backend contract, `luran`)
All under `/api/v1`. Exact schemas finalized in the plan; shapes here are the contract intent.
- **Public request (unauthenticated):** `POST /account/deletion-request` `{email}` → **always 200** `{status: "if an account exists, a verification email has been sent"}` (never leak whether the email exists; rate-limited to deter abuse/enumeration).
- **Public verify:** `POST /account/deletion-request/verify` `{token}` (or `GET` link) → moves the request to `grace`, returns what-happens-next + the grace end date.
- **Authed self-serve request:** `POST /account/deletion-request` with a valid bearer token (no email needed) → creates the request in `grace`, disables the account, emails a confirmation + cancel link. Enforces the **last-admin guard** (409 with a machine-readable reason `last_admin` when blocked, listing options).
- **Authed whole-tenant delete (last-admin explicit):** `POST /account/deletion-request/tenant` (tenant_admin only, explicit confirm token) → schedules the tenant cascade.
- **Cancel:** `POST /account/deletion-request/cancel` `{token}` (from the email link) or authed → restores the account if still in grace; 409 if already purged.
- **Ownership transfer** (to satisfy the last-admin guard without deleting the tenant): reuse/extend the existing team/role endpoints to promote another member to `tenant_admin`.

## 5. Dashboard (`luran-dashboard`)
- **Public page** `/delete-account` (unauthenticated, linked from footer + given to Google Play): explains consequences + the deleted-vs-retained policy, an **email** input, submit → "check your email" (same copy regardless of whether the account exists). A verify landing page for the emailed link.
- **Authed self-serve:** Settings → "Delete account" → a confirm dialog stating consequences → creates the request; surfaces the **last-admin** case with the two options (transfer ownership, or delete the whole tenant behind a second explicit confirm). A "cancel pending deletion" affordance while in grace.

## 6. Security / privacy
- Public endpoint: constant-time, existence-non-leaking responses; strict rate-limit + captcha-optional; signed short-TTL tokens.
- Verification + cancel tokens are single-use, expiring, bound to the request.
- Session revocation on disable (existing `revoked:{jti}`).
- Whole-tenant deletion always requires an explicit second confirmation and admin auth — never reachable from the public path.

## 7. Testing / verification
- Backend: last-admin guard (block + the two options); support-agent self-delete; lifecycle (request→verify→grace→cancel restores; grace→purge anonymizes + retains the defined subset; purged is irreversible); public endpoint never leaks existence + is rate-limited; tenant cascade deletes all member data. Live e2e against a running app per the repo's e2e harness where feasible.
- Dashboard: public form (success copy identical for exists/not-exists), verify + cancel landings, authed flow incl. last-admin branching.
- An **fe-integration-guide** for the new endpoints (captured against the running app) per the repo standard.

## 8. Out of scope / follow-ups
- In-app (agent app) deep-link to the public deletion URL (Play also likes an in-app path) — follow-up.
- Final legal sign-off on the exact deleted-vs-retained field list and grace length.

## 9. Build split (parallelizable)
1. **Backend (`luran`)** — schema (`deletion_requests` + `User.deletion_status`), endpoints, last-admin guard, tenant cascade, purge worker, emails. Contract-first. (Its own spec→plan→build.)
2. **Dashboard (`luran-dashboard`)** — public page + authed Settings flow, against the backend contract. (Its own plan→build after the backend contract is real.)
