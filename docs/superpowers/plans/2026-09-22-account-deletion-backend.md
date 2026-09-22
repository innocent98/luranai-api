# Account Deletion — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Backend for account deletion: request → verify → 30-day cancellable grace → purge (anonymize PII + retain minimal legal subset), with a last-admin guard and a tenant-cascade path, exposed via authed self-serve + a public non-leaking request endpoint.

**Architecture:** A `DeletionRequest` record drives a lifecycle; a deletion service owns request/verify/cancel + the last-admin guard; a purge service owns the irreversible anonymization + tenant cascade; endpoints wire auth + rate-limiting + non-leaking responses; an ARQ worker runs due purges. Reuses the existing email + auth + session-revocation infra.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, ARQ, Redis, pytest, ruff, black (via poetry — match `poetry.lock` pins).

**Spec:** `docs/superpowers/specs/2026-09-22-account-deletion-design.md`

## Global Constraints
- Clean commits — NO AI-attribution trailer/footer. Author solely Adebayo.
- Feature branch `feat/account-deletion-backend` off `develop`; PR → develop. Do NOT commit to develop/main directly.
- Reproduce CI locally and make green BEFORE pushing: `poetry run ruff check app/`, `poetry run black --check app/`, `poetry run pytest tests/` (match the exact CI steps + pinned tool versions).
- Alembic: exactly one new migration, chaining off current head `z3a4b5c6d7e8`; prove upgrade→downgrade→upgrade round-trips on a fresh DB. Single head.
- **Irreversibility discipline:** no PII in logs; purge is idempotent; every destructive path requires the request to be in the correct state; the public endpoint must be constant-shape and MUST NOT leak whether an email exists.
- **Legal sign-off (pre-launch, flag don't block):** the exact deleted-vs-retained field list in Task 3 is a proposed default — mark it clearly for compliance review before production launch.
- Grace period from config `ACCOUNT_DELETION_GRACE_DAYS` (default 30).
- Anchors: `app/db/models/user.py` (User: encrypted `email`, `email_hash`, `full_name`, `role`, `is_active`); `app/api/deps.py` (`get_current_user`:71, `get_current_tenant_admin`:166, `get_current_agent`:178); ARQ worker pattern `app/workers/conversation_auto_resolver.py`; email `app/services/email_service.py`; session revoke `revoked:{jti}` (`app/api/deps.py`); team/role for ownership transfer `app/api/v1/endpoints/users.py`.

---

### Task 1: Schema — `DeletionRequest` + `User.deletion_status` + migration

**Files:** Create `app/db/models/deletion_request.py`; modify `app/db/models/user.py`; create `alembic/versions/<rev>_add_account_deletion.py`; Test `tests/db/test_deletion_request_migration.py` (or the repo's migration-test location).

**Interfaces — Produces:**
- `DeletionStatus` enum: `active`, `pending_verification`, `grace`, `purged` (str values).
- `User.deletion_status: DeletionStatus` (default `active`), `User.deletion_requested_at: datetime | None`.
- `DeletionRequest` model: `id`, `user_id` (FK), `tenant_id`, `scope` enum (`self_user` | `tenant`), `status` (`DeletionStatus`), `verification_token_hash: str | None`, `cancel_token_hash: str | None`, `requested_via` (`authed` | `public`), `scheduled_purge_at: datetime | None`, `created_at`, `updated_at`.

- [ ] **Step 1: Write failing migration round-trip test** — fresh DB: `alembic upgrade head` creates `deletion_requests` + the new `users` columns + the `deletion_status` enum; `downgrade -1` removes them; `upgrade head` again succeeds. Assert single head.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the enum + models (mirror existing model style in `app/db/models/`), and author the migration (chain off `z3a4b5c6d7e8`; create enum, table, columns; reversible downgrade). Follow the repo's enum conventions carefully (see the prior `message_role` casing lesson — ensure the SQLAlchemy `Enum` name/values are consistent).
- [ ] **Step 4: Run test, verify pass.**
- [ ] **Step 5: `ruff`+`black`; commit** — `feat(db): account-deletion schema + migration`.

---

### Task 2: Deletion request service — create / verify / cancel + last-admin guard

**Files:** Create `app/services/account_deletion_service.py`; Test `tests/services/test_account_deletion_service.py`.

**Interfaces — Produces (`AccountDeletionService`):**
- `request_self(user) -> DeletionRequest` — authed self-serve: enforces the **last-admin guard** (raise a typed `LastAdminError` carrying options when `user` is the sole remaining `tenant_admin`), else creates a `grace` request (`scope=self_user`, `scheduled_purge_at = now + grace`), sets `user.deletion_status=grace`, revokes the user's sessions.
- `request_public(email) -> None` — resolves via `email_hash`; if a user exists, creates a `pending_verification` request + returns a raw verification token (to email); ALWAYS returns None regardless (caller responds identically). Same last-admin guard applies at verify time.
- `verify(token) -> DeletionRequest` — `pending_verification → grace` (set purge date, disable account, revoke sessions); invalid/expired token → typed error.
- `cancel(token_or_user) -> None` — `grace → active` restore (clear `deletion_status`, re-enable) if not yet purged; already `purged` → typed error.
- `request_tenant(admin) -> DeletionRequest` — the last-admin explicit whole-tenant path (`scope=tenant`), tenant_admin only.
- Tokens stored HASHED (like passwords); raw token returned once for emailing.

- [ ] **Step 1: Write failing tests** — (a) sole tenant_admin `request_self` → `LastAdminError` with options, no request created; (b) admin with a co-admin → `grace` request + `deletion_status=grace` + sessions revoked; (c) support agent `request_self` → `grace` request; (d) `request_public` for existing email → creates `pending_verification` + a token; for non-existent email → no request, returns None (no throw); (e) `verify` valid token → `grace`; invalid/expired → error; (f) `cancel` in grace → restored `active`; after purged → error. Use the repo's DB test fixtures.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the service (reuse token hashing like `app/services/token_service.py`/auth; session revoke via the `revoked:{jti}` mechanism). No purge logic here.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: `ruff`+`black`; commit** — `feat(account-deletion): request/verify/cancel service + last-admin guard`.

---

### Task 3: Purge + anonymization + tenant cascade (irreversible)

**Files:** Create `app/services/account_purge_service.py`; Test `tests/services/test_account_purge_service.py`.

**Interfaces — Produces (`AccountPurgeService`):**
- `purge_user(user_id) -> None` — anonymize the user: null/scrub `email` (and `email_hash` → a non-reversible tombstone value), `full_name`, phone, avatar, auth credentials, device push tokens; set `deletion_status=purged`; keep the row as a tombstone so FKs (e.g. `ticket.assigned_to`) don't dangle; anonymize authored attribution to a generic label. **Idempotent** (re-running on a purged user is a no-op). Retain the minimal legal subset (billing/invoice/legal-hold) linked to the tombstone — **PROPOSED list; flag for legal sign-off** (see Global Constraints).
- `purge_tenant(tenant_id) -> None` — cascade: delete/anonymize all tenant data (sessions, tickets, leads, KB, config, sales config, devices) and all member users per `purge_user`. Idempotent.

- [ ] **Step 1: Write failing tests** — (a) `purge_user` nulls/anonymizes the PII fields listed, sets `purged`, KEEPS the row (tombstone) and does NOT break a referencing ticket's FK; (b) idempotent (second call no-op, no error); (c) the defined retained subset survives (assert a billing/legal record still resolves via the tombstone); (d) `purge_tenant` removes all member users + tenant-scoped data. Use DB fixtures with a user referenced by a ticket.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Be exhaustive about which columns are PII vs retained; centralize the field list so it's auditable. No PII in logs.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: `ruff`+`black`; commit** — `feat(account-deletion): purge/anonymization + tenant cascade`.

---

### Task 4: API endpoints + emails + rate-limit + non-leaking responses

**Files:** Create `app/api/v1/endpoints/account_deletion.py` (register in the API router); use `app/services/email_service.py`; Test `tests/api/v1/test_account_deletion_api.py`.

**Interfaces — Produces (all `/api/v1`):**
- `POST /account/deletion-request` — dual-mode: with bearer (`get_current_user`) → `request_self` (returns the grace end + emits confirmation+cancel email); returns 409 `{code:"last_admin", options:[...]}` on `LastAdminError`. Without bearer, with `{email}` body → `request_public` + verification email; **ALWAYS 200** with a fixed body (`"if an account exists, a verification email has been sent"`); **rate-limited** (reuse the app's limiter). 
- `POST /account/deletion-request/verify` `{token}` → `verify` → 200 with grace end date.
- `POST /account/deletion-request/cancel` `{token}` (or authed) → `cancel` → 200; 409 if purged.
- `POST /account/deletion-request/tenant` (`get_current_tenant_admin`, explicit `{confirm: true}`) → `request_tenant`.

- [ ] **Step 1: Write failing tests** — (a) authed self-serve request → 200 + request in grace + confirmation email sent (assert email service called); (b) authed sole-admin → 409 `last_admin` with options; (c) public request existing vs non-existent email → **byte-identical 200 body** either way, email sent only when it exists; (d) public endpoint rate-limited; (e) verify/cancel happy + error paths; (f) tenant delete requires admin + confirm. Mirror existing endpoint tests + the app's rate-limit test fixtures.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** endpoints + wire the deletion service + email templates (reuse `email_service`). Constant-shape public response; no PII in errors.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: `ruff`+`black`; commit** — `feat(account-deletion): API endpoints + emails`.

---

### Task 5: ARQ purge worker + registration

**Files:** Create `app/workers/account_deletion_purge.py`; register in the ARQ worker settings/cron (find it — mirror how `conversation_auto_resolver` is scheduled); Test `tests/workers/test_account_deletion_purge.py`.

**Interfaces — Consumes:** `AccountPurgeService` (Task 3), `DeletionRequest` (Task 1).

- [ ] **Step 1: Write failing test** — a due request (`status=grace`, `scheduled_purge_at <= now`) gets purged (calls `purge_user`/`purge_tenant` per scope, sets `status=purged`); a not-yet-due grace request is left untouched; an already-`purged` request is skipped. Mirror `conversation_auto_resolver`'s test style.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the worker (query due requests, dispatch to the purge service by scope, mark purged) and register it on the same cron cadence as the auto-resolver.
- [ ] **Step 4: Run test, verify pass.**
- [ ] **Step 5: `ruff`+`black`; full `pytest`; commit** — `feat(account-deletion): scheduled purge worker`.

---

## Self-Review
- **Spec coverage:** schema (T1); request/verify/cancel + last-admin guard (T2); anonymize+retain purge + tenant cascade (T3); public non-leaking + authed endpoints + emails + rate-limit (T4); scheduled purge (T5); disclosure copy lives in the dashboard (separate plan). ✅
- **Type consistency:** `DeletionStatus`/`DeletionRequest` (T1) used by T2/T3/T5; `AccountDeletionService` methods (T2) consumed by T4; `AccountPurgeService` (T3) consumed by T5. ✅
- **Flagged:** exact retained-field list (T3) needs legal sign-off pre-launch; ownership-transfer to satisfy the last-admin guard reuses `users.py` team/role endpoints (confirm they support promoting a member to tenant_admin; if not, that's a small addition — implementer reports).
- **Deferred to the dashboard plan:** the public `/delete-account` page, the authed Settings flow, and the deleted-vs-retained disclosure copy.
