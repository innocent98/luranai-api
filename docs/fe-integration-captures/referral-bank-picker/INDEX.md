# Referral bank picker: live captures (FAKE Paystack)

Recorded by `scripts/capture_bank_picker.py` against a locally running app whose `PAYSTACK_BASE_URL` pointed at `scripts/fake_paystack.py`. **Luran's real endpoints were exercised; Paystack was not.** The fake follows Paystack's public docs, so these files prove Luran's wire contract, not Paystack's real behaviour.

| File | Call | Status | What it shows |
|---|---|---|---|
| `01-super-admin-login.json` | `POST /api/v1/platform/auth/login` | 200 | Platform admin signs in (cookies). |
| `02-plain-admin-login.json` | `POST /api/v1/platform/auth/login` | 200 | Platform admin signs in (cookies). |
| `03-banks-paystack-down-no-cache.json` | `GET /api/v1/platform/referrals/banks` | 503 | Bank list while Paystack is down and nothing is cached. |
| `04-banks-ok.json` | `GET /api/v1/platform/referrals/banks` | 200 | Bank list: active banks only, {name, code}, sorted by name, fetched over two Paystack pages. |
| `05-banks-cached-while-paystack-down.json` | `GET /api/v1/platform/referrals/banks` | 200 | Second call is served from the 24 h cache although Paystack is down again. |
| `06-banks-plain-admin.json` | `GET /api/v1/platform/referrals/banks` | 200 | A plain ADMIN may read the bank list. |
| `07-banks-no-auth.json` | `GET /api/v1/platform/referrals/banks` | 401 | No credentials. |
| `08-resolve-ok.json` | `POST /api/v1/platform/referrals/banks/resolve` | 200 | Account resolves: only the holder name comes back. |
| `09-resolve-not-found.json` | `POST /api/v1/platform/referrals/banks/resolve` | 422 | Paystack says no such account (HTTP 422 from the fake). |
| `10-resolve-paystack-error.json` | `POST /api/v1/platform/referrals/banks/resolve` | 503 | Paystack answers 500. |
| `11-resolve-paystack-throttled.json` | `POST /api/v1/platform/referrals/banks/resolve` | 503 | Paystack answers 429. |
| `12-resolve-bad-account-number.json` | `POST /api/v1/platform/referrals/banks/resolve` | 422 | Account number is not 10 digits (rejected before any Paystack call). |
| `13-resolve-bad-bank-code.json` | `POST /api/v1/platform/referrals/banks/resolve` | 422 | Bank code empty. |
| `14-resolve-plain-admin.json` | `POST /api/v1/platform/referrals/banks/resolve` | 403 | A plain ADMIN may not resolve accounts. |
| `15-resolve-no-auth.json` | `POST /api/v1/platform/referrals/banks/resolve` | 401 | No credentials. |
| `16-resolve-rate-limit-1.json` | `POST /api/v1/platform/referrals/banks/resolve` | 200 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `17-resolve-rate-limit-2.json` | `POST /api/v1/platform/referrals/banks/resolve` | 200 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `18-resolve-rate-limit-3.json` | `POST /api/v1/platform/referrals/banks/resolve` | 200 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `19-resolve-rate-limit-4.json` | `POST /api/v1/platform/referrals/banks/resolve` | 200 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `20-resolve-rate-limit-5.json` | `POST /api/v1/platform/referrals/banks/resolve` | 429 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `21-resolve-rate-limit-6.json` | `POST /api/v1/platform/referrals/banks/resolve` | 429 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `22-resolve-rate-limit-7.json` | `POST /api/v1/platform/referrals/banks/resolve` | 429 | Resolve again until the per-admin budget (10 per minute) runs out. |
| `23-resolve-rate-limit-8.json` | `POST /api/v1/platform/referrals/banks/resolve` | 429 | Resolve again until the per-admin budget (10 per minute) runs out. |
