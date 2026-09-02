# SOP — Agent WebSocket: accept JWT from cookie or query token

**Status:** Shipped (branch, pre-PR) · `fix/agent-ws-cookie-auth` · 2026-08-30
**Area:** `app/api/v1/endpoints/websocket.py` (`agent_websocket`), `tests/api/v1/test_agent_websocket_auth.py`

---

## What shipped

The agent WebSocket endpoint `/api/v1/ws/agent/{session_id}` now authenticates the agent JWT
from **either** the `?token=` query param **or** the httponly `access_token` cookie. Previously it
required the query param, so the web dashboard — which opens the socket with `new WebSocket(url)`
and relies on the browser attaching the `access_token` cookie automatically (exactly as it does
for REST) — was rejected at the handshake before the handler ever ran.

Only the **source** of the token changed. The JWT, its `SECRET_KEY`/algorithm, every claim
(`sub`, `tenant_id`), and every authorization check (agent exists + active, role in
`{TENANT_ADMIN, SUPPORT_AGENT}`, session-tenant match, subscription active) are byte-for-byte
unchanged.

Because accepting a cookie introduces a Cross-Site WebSocket Hijacking (CSWSH) risk, the
**cookie path is Origin-gated** against `settings.BACKEND_CORS_ORIGINS`; the query-token path is
not. See "CSWSH mitigation" below.

## CSWSH mitigation (security addition)

**Finding (HIGH):** an automated security review flagged the first cut of this change. The
`access_token` cookie is an *ambient* credential — the browser attaches it automatically — and
WebSocket handshakes are exempt from CORS. Without an Origin check, a malicious cross-origin page
could open `wss://…/ws/agent/{id}`; the victim agent's browser would attach the cookie, and the
attacker would authenticate as the victim (CSWSH). The original `?token=`-only design was
CSWSH-resistant precisely because the token is a secret a cross-origin page cannot read.

**Mitigation:** the two sources are gated by their trust properties:

- **`?token=` (query bearer secret):** accepted with **no** Origin check. It is a secret an
  attacker page cannot read, and the Flutter app sends no `Origin` header — gating it would break
  the native app.
- **`access_token` cookie (ambient credential):** requires a trusted `Origin`. The handshake reads
  `websocket.headers.get("origin")` and, if it is missing or not in `settings.BACKEND_CORS_ORIGINS`
  (the **same** allowlist REST CORS enforces in `app/main.py`), closes with `4403 "Origin not
  allowed"` before any decode. No wildcards.

The `Origin` header cannot be forged by a cross-origin browser context (it is set by the browser,
not JS), so an exact-match against the trusted-frontends allowlist closes the hijack vector while
leaving the legitimate dashboard (whose origin is already in the allowlist) working.

## Why

**Symptom:** staging logs showed `"WebSocket /api/v1/ws/agent/{id}" 403` repeating every ~11s; the
dashboard silently fell back to polling `/messages` and never got true WS realtime.

**Root cause:** `token: str = Query(...)` made the query param *required*. Starlette rejects a
missing required query param with a 403 **before** the handler body runs. The Flutter app passes
`?token=<jwt>` and worked; the dashboard's browser cannot read the httponly cookie in JS, so its
client sends no query token and depends on the cookie being sent automatically — which the
endpoint never consulted.

**User-visible effect of the fix:** the dashboard's WS handshake now succeeds, so it receives live
customer messages/typing/agent-join events over the socket instead of polling, and `connect_agent`
(which notifies the customer that a human agent joined) runs on the dashboard path.

## How

`agent_websocket` signature changed from `token: str = Query(...)` to
`token: str | None = Query(None, ...)`, and before the decode:

```python
origin = websocket.headers.get("origin")
if token:
    effective_token = token            # query bearer secret; app sends no Origin; CSWSH-safe
else:
    if not origin or origin not in settings.BACKEND_CORS_ORIGINS:
        await websocket.close(code=4403, reason="Origin not allowed")   # CSWSH guard
        return
    effective_token = websocket.cookies.get("access_token")
if not effective_token:
    await websocket.close(code=4003, reason="Missing authentication")
    return
```

`jwt.decode(...)` now decodes `effective_token`. The rejection behavior for a missing token is the
same `4003` close the endpoint already used for a bad token (previously the framework returned 403
for the missing *required* param; now the handler owns that rejection with a clear reason). A
disallowed/missing Origin on the cookie path closes with `4403` before any decode.

The cookie name (`access_token`), attrs (httponly, secure, samesite=lax, path=/), and REST auth
pattern were confirmed against `app/core/security.py::set_auth_cookies` and
`app/api/deps.py::extract_token` / `get_current_user`. This is the same cookie-first pattern REST
auth already uses; the WS endpoint now mirrors it (query-param fallback instead of Bearer header).

### Sibling endpoint — `authenticated_customer_websocket` (checked, deliberately unchanged)

`authenticated_customer_websocket` (`/customer/{session_id}`) has the same structural
query-token-only pattern, but its client — the `luran-chat` widget — is a **cross-origin,
third-party embed** (`cdn.luranai.com` loaded on customer sites). A `samesite=lax` cookie is not
sent on cross-site WebSocket requests, so cookie auth is structurally unavailable to the widget;
customer login also returns `access_token` in the response body (`customer_auth.py::login_customer`),
so the widget passes it as `?token=`. It therefore does **not** exhibit the dashboard's bug. Left
unchanged to keep this fix scoped. See follow-ups.

## What's involved

| File | Change |
|---|---|
| `app/api/v1/endpoints/websocket.py` | `agent_websocket` auth block: optional query token + `access_token` cookie fallback; **cookie path Origin-gated against `BACKEND_CORS_ORIGINS` (4403)**; decode `effective_token`; explicit `4003 "Missing authentication"` when both absent. Comments explaining app-vs-dashboard token source and the CSWSH split. |
| `tests/api/v1/test_agent_websocket_auth.py` | New: 10 tests — cookie-auth success from an allowed Origin, query-token success (no Origin), query token bypasses the Origin gate, cookie from disallowed/missing Origin → 4403, and 4003 for missing / invalid / expired token via each source. |

No schema, config, or dependency changes.

## Verification

Two-sided test (ran the new suite against the **pre-fix** endpoint, then the fixed one):

- **Pre-fix:** `authenticates_via_cookie`, `rejects_when_no_token_present`, `rejects_invalid_cookie_token`,
  `rejects_expired_cookie_token` **FAIL** (bug reproduced — cookie ignored, missing/bad cookie yields
  the framework's non-4003 rejection); the three query-token tests **PASS** (unchanged path).
- **Post-fix:** all **7 pass**.

CI checks reproduced locally with the pinned toolchain (ruff 0.8.6, black 24.10.0) and CI env vars,
against Postgres 15 + Redis 7 containers:

| CI job | Command | Result |
|---|---|---|
| lint | `poetry run ruff check app/` | All checks passed |
| lint | `poetry run black --check app/` | 228 files unchanged |
| test | `poetry run pytest tests/` (Postgres+Redis) | 1178 passed, 33 skipped |
| automation-flow-regression | `pytest tests/api/v1/test_chat_flow_router.py tests/api/v1/test_flow_action_call_regressions.py` (sqlite) | 14 passed |

The CSWSH guard is covered directly: `test_agent_ws_cookie_disallowed_origin_rejected_4403` and
`test_agent_ws_cookie_missing_origin_rejected_4403` prove a cross-origin/originless cookie handshake
is closed before decode, while `test_agent_ws_query_token_bypasses_origin_gate` proves a valid query
token is still accepted regardless of Origin (native app unbroken).

`build` (Docker image) and `security-scan` (Trivy) are non-code CI jobs and were not run locally.

## Operate / roll back

Pure code change to one endpoint. Roll back by reverting the commit; no data or migration impact.
No config/env changes needed to deploy.

## Follow-ups

- Confirm against the `luran-chat` widget source that `authenticated_customer_websocket` is only
  reached with a query `?token=` (cross-origin embed). If any **same-origin** customer surface is
  ever added that relies on the cookie, apply the identical cookie-fallback **and the same
  Origin gate** there (a cookie path without the Origin gate would reintroduce CSWSH).
- FE (dashboard): no change required — the fix makes the existing cookie-based `new WebSocket(url)`
  handshake succeed. The dashboard can drop its `/messages` polling fallback once realtime is
  confirmed on staging.
