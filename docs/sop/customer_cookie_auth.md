# SOP — Customer (B2C) session: cookie-only auth + customer WebSocket cookie auth

**Status:** PR open (not merged) · `feat/customer-cookie-auth` · 2026-10-14
**Area:** `app/api/v1/endpoints/customer_auth.py`, `app/api/deps.py`, `app/core/security.py`,
`app/api/v1/endpoints/websocket.py` (`authenticated_customer_websocket`)
**Tests:** `tests/api/v1/test_customer_cookie_auth.py`, `tests/api/v1/test_customer_websocket_cookie_auth.py`
**Pairs with:** luran-chat PR `feat/customer-cookie-auth` (see the PR cross-reference).

## What shipped

The authenticated customer session is carried only by HttpOnly cookies, and the
authenticated customer WebSocket accepts that cookie.

| Change | Before | After |
|---|---|---|
| Cookie names | shared `access_token` / `refresh_token` (same names as the tenant dashboard, same API host) | dedicated `customer_access_token` (Path `/`, 15 min) and `customer_refresh_token` (Path `/api/v1/customer/auth`, 7 d) |
| Login/refresh response body | included `access_token` | `token_type`, `expires_in` (+ `customer` on login) only; no token in the body |
| Refresh cookie path | `/api/v1/customer/auth/refresh` | `/api/v1/customer/auth` so it also reaches `/logout` |
| `GET /ws/customer/{id}` | required `?token=` | `?token=` optional; otherwise the cookie, gated by `Origin` against `BACKEND_CORS_ORIGINS` |
| WS token revocation | not checked | `jti` revocation checked (logout now closes the door on the socket too) |

## Why

Security audit (2026-04-12): customer tokens readable by JS means XSS leads to account takeover.
luran-chat had already stopped persisting the token (commit `e792294`), but three server-side gaps
remained:

1. The customer cookies reused the tenant cookie names. One browser logged into both the dashboard
   and the customer app would have one login overwrite the other, and `extract_token` (cookie first)
   would hand a customer JWT to tenant endpoints.
2. `logout` could never revoke the refresh token: the refresh cookie was scoped to `/refresh`, so the
   browser did not send it to `/logout`. The refresh token stayed valid server-side after logout.
3. The authenticated customer WebSocket only accepted `?token=`, so once the frontend dropped the
   token it had no way to authenticate (it fell back to the public visitor socket).

## How / decisions

- **Dedicated names, not a flag.** `set_auth_cookies` / `clear_auth_cookies` gained optional cookie-name
  arguments (defaults unchanged, so the tenant and platform flows are untouched).
- **Legacy migration, no forced logout.** `extract_customer_token` reads `customer_access_token`, then a
  Bearer header, then the legacy `access_token` cookie (still validated as `auth_type=customer`).
  `/refresh` falls back to the legacy `refresh_token` cookie. Login, refresh and logout expire the
  legacy customer-path refresh cookie; the legacy `access_token` is expired only when it holds a
  customer JWT, so a tenant dashboard session is never touched. The fallbacks can be deleted one
  refresh-token lifetime (7 d) after rollout.
- **Bearer header still accepted** for non-browser clients and the live e2e harness. Not stored by the web app.
- **WebSocket CSWSH:** same rule as the agent socket (#85): the cookie path needs an `Origin` in
  `BACKEND_CORS_ORIGINS`; the `?token=` path does not. Rejections are close-before-accept, which a real
  client sees as an HTTP 403 handshake failure (the 4403/4003 codes are visible to ASGI test clients only).
- **Rejected:** a one-time localStorage-token exchange endpoint. The old token was a 15 minute JWT and the
  frontend stopped storing it in April, so an exchange endpoint adds attack surface for nothing.

## Config and ops

- `BACKEND_CORS_ORIGINS` must contain the luran-chat origin (it already does for REST CORS with credentials);
  the WebSocket Origin gate reuses the same list.
- Cookies are `Secure; HttpOnly; SameSite=Lax`. They are sent because luran-chat and the API are same-site
  (both under `luranai.com`). A different registrable domain for the chat app would need `SameSite=None`.
- No migration, no new env var.

## Verification

- 20 new tests (11 REST cookie, 9 WebSocket) red first, then green. Full suite on throwaway Postgres 15 +
  Redis 7: 1678 passed, 33 skipped. `ruff 0.8.6` and `black 24.10.0` (poetry-pinned) clean on `app/`.
- Live capture against a local uvicorn: `docs/captures/customer-cookie-auth/` (login, me, refresh, rotation
  reuse 401, logout, revoked 401, CORS, WebSocket). Guide: `docs/fe-integration-guide-customer-auth.md`.

## Roll back

Revert the PR. Sessions minted under the new names will 401 once and the user logs in again. Deploy the
backend before luran-chat; the old frontend keeps working against it (it never read the body token).

## Follow-ups

- Remove the legacy-cookie fallbacks about 7 days after rollout.
- The tenant `/auth/logout` has the same refresh-cookie-path gap (`/api/v1/auth/refresh` is not sent to the
  logout path). Out of scope here; worth a separate fix.
- The live e2e harness lifts the cookie manually because it talks plain HTTP (Secure cookies are not replayed).
