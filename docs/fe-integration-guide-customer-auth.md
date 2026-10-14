# FE integration guide — authenticated customer session (cookie-only)

Audience: luran-chat (the customer web app). Every body, header and status below was captured from a
running local API (`uvicorn`, throwaway Postgres 15 + Redis 7, one seeded placeholder customer). Raw files:
`docs/captures/customer-cookie-auth/`. Token values are replaced with `<redacted>` in the captures; nothing else
is edited. The email and ids are placeholders from a throwaway database.

## What it is

The customer signs in; the API sets two HttpOnly cookies. JavaScript never sees a token: no localStorage, no
response body, no `authToken` prop. Every request uses `credentials: 'include'`.

| Cookie | Path | Max-Age | Flags |
|---|---|---|---|
| `customer_access_token` | `/` | 900 | HttpOnly; Secure; SameSite=Lax |
| `customer_refresh_token` | `/api/v1/customer/auth` | 604800 | HttpOnly; Secure; SameSite=Lax |

The tenant dashboard keeps using `access_token` / `refresh_token`; the two sessions no longer collide.

## Endpoints (base `/api/v1`)

### POST `/customer/auth/login`  (capture 01)
Request `{"email": "...", "password": "..."}`. 200:
```
set-cookie: customer_access_token=<redacted>; HttpOnly; Max-Age=900; Path=/; SameSite=lax; Secure
set-cookie: customer_refresh_token=<redacted>; HttpOnly; Max-Age=604800; Path=/api/v1/customer/auth; SameSite=lax; Secure
set-cookie: refresh_token=""; ...; Max-Age=0; Path=/api/v1/customer/auth/refresh; SameSite=lax
{"token_type":"bearer","expires_in":900,"customer":{"id":"98183b10-f352-420a-bce0-28e6fb7fa40c","full_name":"Live Customer","email":"live.customer@example.com","chat_user_key":"WCM3mZwRupxiZb53Ah_q_hDEG-HQs-at","avatar_url":null,"is_active":true,"email_verified_at":"2026-10-14T01:04:29.472711Z","last_login_at":"2026-10-14T01:05:07.843631Z","created_at":"2026-10-14T01:04:29.192572Z","updated_at":"2026-10-14T01:05:07.563037Z"}}
```
Trap: there is **no `access_token` in the body** any more. The third Set-Cookie is the old-cookie cleanup; ignore it.

### GET `/customer/auth/me`  (captures 02, 03)
With the cookie: 200 and the bare customer object (not nested under `customer`):
`{"id":"98183b10-...","full_name":"Live Customer","email":"live.customer@example.com","chat_user_key":"...","avatar_url":null,"is_active":true,...}`.
Without credentials: **401** `{"detail":"Could not validate customer credentials"}` with `www-authenticate: Bearer`.
This is the reload rehydrate call and the "am I signed in" probe.

### POST `/customer/auth/refresh`  (captures 04, 05)
Sends `customer_refresh_token` automatically. 200 `{"token_type":"bearer","expires_in":900}` and both cookies are re-set (refresh token rotates).
Replaying the previous refresh token: **401** `{"detail":"Invalid or expired refresh token"}`. No cookie at all: 401 `{"detail":"No refresh token"}` (verified in the test suite, not in a live capture).
A 401 never carries `Set-Cookie`. Multiple tabs must serialise rotation (Web Lock `luran-customer-refresh`): there is no server grace window, so a second tab replaying a just-rotated token is signed out.
Changing or resetting the password revokes every refresh token (verified in pytest only).

### POST `/customer/auth/logout`  (captures 06, 07, 08)
200 `{"message":"Logged out"}`; `customer_access_token` and `customer_refresh_token` are expired (`Max-Age=0`).
Server side: the access token's `jti` is revoked (then `/me` with it returns 401, capture 07) and the refresh token is revoked
(then `/refresh` returns 401 `{"detail":"Invalid or expired refresh token"}`, capture 08).

### CORS (capture 09)
Responses carry `access-control-allow-credentials: true` and echo an allowed `Origin`. The luran-chat origin must be in
`BACKEND_CORS_ORIGINS`.

## WebSocket `/ws/customer/{session_id}`  (capture 10)

Open it with no query string; the browser attaches `customer_access_token`. The handshake must carry an `Origin` that is
in `BACKEND_CORS_ORIGINS` (browsers do this automatically).

| Case | Result |
|---|---|
| cookie + allowed Origin | connected; `{"type":"ping"}` answered with `{"type":"pong","timestamp":"<ts>"}` |
| cookie + foreign Origin | handshake rejected, HTTP 403 |
| cookie, no Origin | handshake rejected, HTTP 403 |
| no cookie / garbage cookie | handshake rejected, HTTP 403 |
| cookie after logout (revoked) | handshake rejected, HTTP 403 |

Trap: every rejection looks the same to a browser (`onclose` with code 1006, never `onopen`). The distinct close codes
4403 (origin) and 4003 (auth) exist but only ASGI test clients can see them. Treat "closed before ever opening" as
"probably expired access cookie": call `/customer/auth/refresh` once, then reconnect once; if that fails, show the sign-in prompt.

## Field-nesting traps

- Login returns `customer` nested; `/me` returns the customer object at the top level.
- No token fields anywhere in a JSON body. Remove `access_token` from the `CustomerAuthResponse` type.

## State machine / cadence

Access cookie lives 15 minutes. On any 401 from a customer endpoint: one `/customer/auth/refresh`, then retry the request once
(single-flight the refresh so concurrent 401s share it). Refresh 401 means signed out: clear UI state, go to `/account/login`.
Do not poll; there is no background refresh.

## UX consequences to surface

- A user who was signed in before this ships has a legacy session; it keeps working for up to one refresh lifetime (7 days), then they sign in again.
- After logout or an expired session, an open chat socket will not reconnect; show "Your session expired. Sign in again."

## Verification table

| Behaviour | Status |
|---|---|
| login Set-Cookie flags, body without token | verified live (01) |
| /me with cookie, without credentials | verified live (02, 03) |
| refresh rotation, reuse 401 | verified live (04, 05) |
| logout cookie expiry, access `jti` revoked, refresh revoked | verified live (06, 07, 08) |
| CORS credentials headers | verified live (09) |
| WS cookie connect, foreign/no Origin, no/garbage cookie, post-logout | verified live (10) |
| WS close codes 4403 / 4003, expired-token cookie, legacy cookie fallbacks, "No refresh token" body | verified in pytest only, not on the wire |
| Cookies in a real browser, cross-origin fetch with credentials | see the luran-chat PR for the browser run |
