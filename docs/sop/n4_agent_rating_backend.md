# SOP — N4 agent-requested rating and per-agent CSAT, backend (2026-10-13)

**Status:** merged to `develop` via PR #124 (`feat/n4-agent-rating`, merge `53ace7d`), live on staging once `develop` deploys. **Not safe for prod yet:** two blockers below.
**Spec:** `docs/superpowers/specs/2026-10-13-n4-agent-rating-design.md` (N4-D1..D10) · **Plan:** `docs/superpowers/plans/2026-10-13-n4-agent-rating-backend.md` · **FE contract:** `docs/fe-integration-guide-agent-rating.md` (captures in `docs/fe-integration-captures/agent-rating/`).

## What shipped
A customer can rate the human agent who helped them. Agents or admins can request the rating (button), and resolving a ticket with an assignee requests it automatically. The rating is stored against that agent; admins get per-agent CSAT, support agents see only their own ratings. The old AI/idle rating keeps working.

## Why
Ratings belonged to the session, so an agent's work was never measurable, and the public POST only checked `user_identifier`. N4 comes from field feedback (triage 2026-10-10) and makes CSAT (PRD Feature 5.1) measurable per human agent.

## How
| ID | Decision |
|---|---|
| N4-D1 | Extend `session_ratings` (`rated_agent_id`, `source`, `rating_request_id`); uniqueness is one rating per (session, rated party) through two partial unique indexes. Existing rows keep `rated_agent_id` NULL (no attribution backfill). |
| N4-D2 | New `rating_requests` table; a human rating requires a valid unfulfilled unexpired token for that session. The client never sends an agent id. Legacy untokened ratings still work (AI party). |
| N4-D3 | Triggers: manual (assignee or admin; ticket `in_progress` or resolved under 24 h), automatic inside `POST /tickets/{id}/resolve`. Auto-resolver cron and close create nothing. One open request per (session, agent); manual cooldown 10 min (409 `rating_request_cooldown`); a later manual request supersedes the open one; auto-on-resolve reuses an open request. |
| N4-D4 | Delivery: persisted ASSISTANT message plus `rating_requested` (customer only, with token) and `rating_request_created` (agents, no token); `ticket_resolved` / `ticket_closed` gain `rating_request_id` + `rating_state`. Token redacted from every staff message serializer. |
| N4-D5 | Visibility: `GET /agent/me/ratings`, per-agent CSAT in analytics, `GET /reviews/session` filters and scoping (support agents and viewers see own only). |
| N4-D6 | CSAT = percent of ratings scoring 4 or 5, `null` (not 0) with no ratings; default window 30 days on the rating's `created_at`. |
| N4-D7 | Comment at most 500 characters; `session_ratings.comment` becomes encrypted at rest, and the migration encrypts existing rows. |
| N4-D8 | No notifications in v1. |
| N4-D10 | Purge keeps `rated_agent_id` / `rating_requests.agent_id` (user row is a PII-free tombstone), NULLs `requested_by`, tenant purge deletes `rating_requests`. |

Spec conflicts resolved during the plan (rulings N4-R1..R7):
1. `visitor_service.py` bootstrap is the customer's own history, so it stays unredacted.
2. Reuse on auto-resolve sends no new `rating_requested` frame (only a token hash is stored); `ticket_resolved` points at the existing request.
3. The request message is inserted in the same transaction as the request, not through `add_message` (which commits mid-lock).
4. Viewers on `GET /reviews/session` are scoped like support agents.
5. The 429 body is an object (`rating_rate_limited`) via a wrapper around the shared limiter.
6. `rating-status.rating` is the human rating for (session, current assignee).
7. The cooldown index is ascending (Postgres scans it backwards); 404 (not 403) for another tenant's ticket on the two new ticket endpoints.
Later rulings: N4-R8 migration encrypts/decrypts every comment unconditionally and downgrade raises on any decrypt failure (the original "skip if it already decrypts" guard let a public POST plant `gAAAAA...` text and block deploys); N4-R9 the staff-visible visitor credential exposure is accepted for v1 (blocker below).

## What's involved
| Area | Files |
|---|---|
| Migration | `alembic/versions/n4_agent_rating.py` (revision `n4agentrating`, down `sr10screenrec`) |
| Models | `app/db/models/review.py` (`RatingRequest`, `SessionRating` columns, `RatingSource`) |
| Services | `app/services/rating_request_service.py` (mint, cooldown, supersede, reuse, frames, resolve hook), `rating_read_service.py` (stats, enrichment), `review_service.py` (token-gated create, list scoping), `analytics_service.py`, `account_purge_service.py` |
| Endpoints | `app/api/v1/endpoints/ticket_ratings.py` (`POST /tickets/{id}/rating-requests`, `GET /tickets/{id}/rating-status`), `reviews.py`, `agent_ratings.py` (`GET /agent/me/ratings`), `tickets.py` (resolve hook, close fields), `chat.py` (`ratings` on session detail, staff redaction), `analytics.py` |
| Utilities | `app/utils/staff_redaction.py`, `app/utils/http_rate_limiter.py` (`rating_rate_limit`, 20 per hour per IP) |
| Schemas | `app/schemas/{rating,review,analytics,conversation,ticket}.py` |

Migration `n4agentrating`: creates `rating_requests`; adds `rated_agent_id`, `source` (backfilled from `meta.source`, then NOT NULL), `rating_request_id`; swaps the single-rating uniqueness for the two partial unique indexes; adds `ix_session_ratings_tenant_agent_created`; encrypts every existing comment in batches. The relationship `session.rating` is now `session.ratings` (a list).

## Verification
- PR #124: full suite on Postgres 15 + Redis 7, 1640 passed; ruff 0.8.6 and black 24.10.0 clean; the flow regression job passed; 7 of 7 CI checks green. Concurrency tests (double submit, parallel creates) ran on Postgres.
- Opus reviews on the migration, token gate, redaction, list scoping and analytics, plus a final review (fix wave `b85f26f`).
- Live local capture on 2026-10-13 against the running app (75 captures, `docs/fe-integration-captures/agent-rating/`): manual request 201/403/404/409 (cooldown, no agent, already rated, not ratable), rating-status in every reason, auto request on resolve with frame order, reuse, supersede, expiry, persisted message customer vs staff, tokened/legacy submit including 400/409/410/422/429, reload-before-submit, scoped lists, analytics, `/agent/me/ratings`. Unverified items are listed in section 11 of the FE guide.

## Operate / roll back
**Deploy window.** `alembic upgrade head` makes `session_ratings.source` NOT NULL and swaps the unique index. Containers still running the **old** code cannot insert a rating (no `source`) from the moment the migration commits until they are replaced, and they were written against the old unique index. The migration also takes brief locks on `session_ratings` for the whole data step (index drops and creates, comment rewrite). The table is one row per rated session, so the lock is short; run the migrate and the container swap back to back and expect failed rating posts only for the seconds the swap takes.
**`ENCRYPTION_KEY`** must be present in the migration environment in **both** directions (the API container that runs `alembic` has it; a bare shell does not). A wrong key makes upgrade or downgrade fail loudly rather than corrupt data.
**Stop automatic requests** (no migration change): remove or guard the resolve-hook call in `app/api/v1/endpoints/tickets.py` (`auto_request_on_resolve` before `ticket_resolved`) and redeploy. Manual requests stay available. There is no tenant toggle yet.
**Migration downgrade** is blocked while any session has more than one rating (the old schema allowed one). De-duplicate first, keeping the AI row; only Adebayo runs the delete:
`DELETE FROM session_ratings WHERE rated_agent_id IS NOT NULL AND session_id IN (SELECT session_id FROM session_ratings GROUP BY session_id HAVING count(*) > 1);` (review the rows first). Downgrade then drops the new columns and `rating_requests`, decrypts comments and restores `uq_session_rating_per_session`.
**Rollout order.** Everything is additive, so the backend can reach prod before the FEs. Old widgets ignore the new frame and file generic untokened ratings (attribution loss, not breakage); old authed views show the question as plain text. `luran-chat` should follow soon.

## Prod blockers (decide before promoting `develop` to `main`)
1. **Rate-limit client IP behind nginx (I2).** The new `rating_rate_limit` (20 per hour per IP) now also gates the **legacy** rating POST every live widget uses. It keys on `request.client.host`; uvicorn trusts forwarded headers only from 127.0.0.1, so behind Docker/nginx every visitor may share one bucket and ratings would stop platform-wide after 20 per hour. Verify on staging: submit from two networks and inspect the `http_rating:*` Redis keys. Fix with `--forwarded-allow-ips` set to the nginx address (keeping X-Forwarded-For spoof-proof) or change the limiter key. Locally the limiter fired after 20 posts from 127.0.0.1 (capture 75), which proves the limit, not the proxy behaviour.
2. **Staff-visible customer credentials (I1 + N4-R9).** The customer socket authenticates with only `session_id` + `user_identifier`. Staff can read `user_identifier` (session payloads, agent WS replay, `GET /reviews/session` items) and `visitor.public_token` (staff ticket and session responses; the public bootstrap returns the token unredacted). A staff member could open the customer socket, request a rating, read the token and file a score as the customer. Token redaction closes only the casual path. Options: customer-side authentication on the token path, keep `user_identifier` and `public_token` off staff surfaces, or strip `rating_token` from the bootstrap. Accepted for staging.

## Follow-ups (deferred minors and gaps)
Migration (T1): the data step holds its lock for the whole run; `updated_at` is rewritten on touched rows; the NOT NULL `source` deploy window (above); the unique-index guard is only testable on Postgres.
Service (T2, T4, T5): the manual-after-auto cooldown has no test; the autouse `expire_on_commit=False` test scope is broad; no `NOWAIT` lock-release proof; the service has no tenant predicate of its own (callers scope); Redis publish errors are swallowed, so frame order can be lost on a partial Redis failure; the auto-resolver non-regression test inspects source text and is brittle.
Redaction (T3): `model_validate` triggers an unused `ratings` lazy-load; the customer conversation detail gains an empty `ratings[]`; some fields could be `Literal` types; the redaction helper is shallow (top-level `meta` only).
Public POST (T6): the limiter has a check-then-increment race; the 400 strings distinguish "Session not found" from "User identifier does not match session" (enumeration, pre-existing); `rated_agent_id` is returned to the customer; `meta` is unbounded; `deps.get_current_user` is async with sync SQLAlchemy (pre-existing).
Lists and analytics (T7, T8, T9): tenant predicates missing in `agent_names` / `ticket_refs` / `enrich_ratings`; the user lookup in `analytics_service.py` (around line 243) is not tenant-scoped (pre-existing); count and average could be one query; `ratings.get` bound once; no tests for platform-admin unscoped lists or the `session_id` filter; the drilldown window and `/analytics/agents` windowing differ (documented in the FE guide); the OpenAPI description for the new params; `/analytics/agents` lists viewers as agents (pre-existing).
Product: CSAT tile on `GET /analytics/overview` split AI vs human; scrub `session_ratings.user_identifier` and comment when a customer account is deleted (not covered by today's purge); tenant toggle for auto-on-resolve (spec Q6); rating-received WS frame or push (N4-D8 v2); exclude `rating_request` notices from AI context if they confuse the model; one rating per (session, agent) across several tickets in one session (Q7); a hosted visitor-bootstrap redaction decision (blocker 2).
FE: P2 `luran-chat` (widget, hosted page, authed views) and P3 dashboard plus Flutter app; plans to be written from the FE guide. PENDING-HUMAN: authed customer socket live check, real widget reload, device checks.
