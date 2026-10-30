# SOP: Referral bank picker, backend (2026-10-30)

## What shipped

Two platform-admin lookups so the referral marketer forms can offer a bank dropdown and check the account number with Paystack instead of asking for a raw bank code: `GET /api/v1/platform/referrals/banks` and `POST /api/v1/platform/referrals/banks/resolve`. Lookups only: saving a marketer, the stored fields, the payout provider (`manual`) and everything that moves money are unchanged.

| Item | Reference |
|---|---|
| PR (`feature/referral-bank-picker-backend` to `develop`) | see the PR list; paired dashboard PR `feature/referral-bank-picker-dashboard` to `staging` |
| FE contract | `docs/fe-integration-guide-referral.md` section 15; captures `docs/fe-integration-captures/referral-bank-picker/` (23 files, `scripts/capture_bank_picker.py`) |
| Parent SOP | `docs/sop/referral_program_backend.md` |

## Why

The owner found that the admin "new marketer" form asks for a Paystack bank code that nobody knows by heart; a wrong code only shows up when a payout fails. Paystack publishes the bank list and can resolve an account number to its holder name, so the form can offer a name-based select and a confirmation of who owns the account.

## How

| Area | Decision | Why / tradeoff |
|---|---|---|
| **Where the forms live** | Both forms (create marketer, update bank details) are super-admin forms. The marketer API is read-only and has no bank update | no marketer-side endpoint was needed |
| **Stored data** | No schema change. The marketer already stores `bank_code`, `account_number` and `account_name` (all `EncryptedString`); the form now fills `account_name` from the resolved name | the resolved name is stored in the existing field, shown for confirmation before saving, never echoed back after (responses still carry `bank_last4` only) |
| **Verification is not enforced on save** | `POST /` and `PATCH /{id}/bank` still accept any code, number and name | an admin must never be blocked by Paystack being down or in test mode; the manual-entry links on the form are the fallback. Cost: an admin who uses the fallback can save an unverified name |
| **Auth** | List: any platform admin. Resolve: super admin (same people who can save), counted against its own per-admin budget | resolve is an account-holder-name oracle; keep it to the people who need it |
| **Resolve is a POST with a body** | Paystack's own call is a GET with the number in the query string; ours takes JSON | our access logs and any proxy in front never see the number. The number is only in the outbound Paystack URL; `httpx`'s INFO request log (which prints URLs) is pinned to WARNING in `paystack_service.py` |
| **Rate limit** | `bank_resolve_rate_limiter`: 10 calls per minute per admin id (100 on staging and development), via `enforce_identifier_limit` inside a dependency, plus the general `platform_admin_rate_limit` | the existing `"user"` key of `check_rate_limit` reads `request.state.user`, which nothing sets, so it falls back to the caller IP; the new limit really is per admin. Invalid-input calls count too (the limiter runs before body validation) |
| **List source and cache** | `PaystackService.list_banks` calls `GET /bank?country=nigeria&perPage=100&use_cursor=true` and follows `meta.next` (at most 10 pages), then `banks.py` keeps active, non-deleted `nuban` rows, one per code, sorted by name, as `{name, code}`. Cached per process for 24 h | the list is public and identical for everyone, so no Redis; each gunicorn worker refetches once a day. A page limit and a cursor are used because Paystack's default page is small (documented as 50), so an unpaged call would silently truncate the list |
| **Stale copy** | If a refresh fails, the last copy is served with `stale: true` for up to 7 days | a Paystack outage must not take the form down when we already know the list; failures are never cached |
| **Paystack unreachable** | List: 503 `bank_provider_unavailable` with a message that points at manual entry. Resolve: 503 with the same code for timeouts, connection errors, 5xx, 429, 401/403 and a 200 without a name. Not-found (Paystack 400/404/422) is 422 `account_not_resolved` | one retryable code for the FE; our message is fixed and Paystack's text is never relayed (it can echo the account number). Timeouts are 8 s read / 4 s connect so the form never hangs for the 30 s payout timeout |
| **Test key** | `test_mode: true` when the key starts `sk_test_`; the form shows a notice. Paystack's documented test-mode account resolution is limited, so a failed name check there is expected | the flag reveals only the key *prefix class*, never the key |
| **SSRF helper** | Not used | the host is the fixed `PAYSTACK_BASE_URL` setting; nothing the caller sends picks the host. The caller-controlled values are a 10-digit number and a short alphanumeric code, sent as query parameters |
| **Logging** | Failures log the HTTP status only: no number, no Paystack message, no headers | proved by `test_resolve_never_logs_the_account_number_or_key` and by grepping the app log of the live run |

## What's involved

| Path | Change |
|---|---|
| `app/services/paystack_service.py` | `list_banks`, `resolve_account`; `_transfers_request` gained `params` and `timeout`; httpx logger pinned to WARNING |
| `app/services/referral/banks.py` (new) | cache, filtering, error mapping (`BankProviderUnavailable`, `AccountNotResolved`) |
| `app/api/v1/endpoints/platform/referrals.py` | `GET /banks`, `POST /banks/resolve`, `get_paystack_service` dependency |
| `app/schemas/referral.py` | `BankOption`, `BankListResponse`, `ResolveAccountRequest`, `ResolveAccountResponse` |
| `app/utils/http_rate_limiter.py` | `bank_resolve_rate_limiter` |
| `tests/referral/test_bank_picker.py` (new) | 34 tests with a fake Paystack (`httpx.MockTransport`) |
| `scripts/fake_paystack.py`, `scripts/capture_bank_picker.py` (new) | the live-run harness |

No migration, no new environment variable, no new dependency.

## Verification

- `tests/referral/test_bank_picker.py`: list (sorted, active only, `{name, code}` only, cursor pages, `test_mode`), cache (24 h expiry, stale up to 7 days then 503, failures not cached, empty list treated as unavailable), resolve (ok, 400/404/422 to `account_not_resolved`, 401/403/429/5xx/timeout/connect/no-name to 503, bad input never reaches Paystack), logs free of number and key, auth (401, plain admin 403 on resolve, 200 on list), rate limit 429. Whole `tests/referral` run: 585 passed; `ruff check app/`, `black --check app/` clean.
- Live run: app on a scratch Postgres (`ref_capture`, alembic head) and Redis, `PAYSTACK_BASE_URL` pointed at `scripts/fake_paystack.py`: 23 captures, all as expected; app and fake logs contain neither the account number nor the key.
- **Not verified: the real Paystack.** No Paystack key was available to the build. The cursor shape, the page size and the error texts are as Paystack's public docs describe them (recalled; the docs site could not be fetched), and the code treats an unexpected shape as "unavailable" rather than guessing.

## Operate / roll back

- Needs only the existing `PAYSTACK_SECRET_KEY` (live key in prod gives `test_mode: false`). Nothing to configure.
- The cache is in process memory: restart the API to force a refresh.
- Roll back: revert the PR. The forms keep working with the old raw bank code input once the dashboard PR is also reverted (or alone: the dashboard falls back to manual entry when these endpoints 404/503).

## Follow-ups

- **Owner, once:** with a live key, call `GET /banks` and `POST /banks/resolve` for one real account of your own and compare with the shapes in FE guide section 15; if Paystack's list is not cursor-paged the way assumed, the list could be short (the form's manual link still works). Capture the real shapes into `docs/fe-integration-captures/referral-bank-picker/`.
- Decide later whether saving should require a verified name (currently not, deliberately).
- The mobile-money / non-NUBAN banks are dropped from the list; revisit if marketers need them.
